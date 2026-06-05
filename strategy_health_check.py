"""每月策略體檢 — 美股情報站

執行: python strategy_health_check.py
排程: Task Scheduler 每月 1 號 8:00

跑近 3 個月所有策略的回測,標記:
  ✅ alive  — alpha > +5 且 trades >= 5
  ⚠️ marginal — 0 < alpha <= +5 或 trades < 5
  ❌ dead   — alpha <= 0 或 trades == 0

推 Telegram 含:當前 regime、活著的策略、淘汰名單、推薦組合。

避免「看舊回測無腦用,結果策略已死」這個陷阱。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

import requests

ROOT = Path(__file__).resolve().parent
TELE_FILE = ROOT / "telegram.json"
SERVER_URL = os.environ.get("US_INTEL_URL", "http://localhost:18506")

STRATEGIES = [
    "score", "score_adaptive", "momentum", "meanrev",
    "golden_cross", "hot_group", "new_high", "rs_rotation",
    "earnings_drift", "win_rate",
]
PARAMS = {"hold_days": 10, "n_positions": 3, "threshold": 30}
LOOKBACK_MONTHS = 3


def load_telegram():
    try:
        return json.loads(TELE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def send_telegram(text: str) -> bool:
    cfg = load_telegram()
    token = cfg.get("bot_token", "")
    chat = cfg.get("chat_id", "")
    if not token or not chat:
        print("[health_check] telegram.json 未設定")
        return False
    plain = text.replace("*", "").replace("_", "")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": plain, "disable_web_page_preview": True},
            timeout=10)
        if r.status_code != 200:
            print(f"[health_check] telegram {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"[health_check] tg fail: {e}")
        return False


def ensure_server_running(max_wait: int = 60) -> bool:
    try:
        r = requests.get(f"{SERVER_URL}/api/stocks", timeout=5)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    server_py = ROOT / "server.py"
    if not server_py.exists(): return False
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    py_exe = str(pythonw) if pythonw.exists() else sys.executable
    try:
        subprocess.Popen(
            [py_exe, str(server_py)], cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                          getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception:
        return False
    for _ in range(max_wait):
        time.sleep(1)
        try:
            if requests.get(f"{SERVER_URL}/api/stocks", timeout=3).status_code == 200:
                return True
        except Exception:
            continue
    return False


def api(path: str, timeout: int = 120):
    r = requests.get(f"{SERVER_URL}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def classify(alpha: float, n_trades: int) -> str:
    if n_trades == 0:           return "dead_no_trades"
    if alpha <= 0:              return "dead"
    if alpha <= 5 or n_trades < 5: return "marginal"
    return "alive"


def main():
    if not load_telegram():
        print("[health_check] telegram.json 沒設,放棄"); sys.exit(0)
    if not ensure_server_running():
        send_telegram("⚠️ 策略體檢失敗 — 無法啟動 server"); sys.exit(1)

    now = datetime.now()
    start = (now - timedelta(days=LOOKBACK_MONTHS * 30)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    # 1. 抓 regime
    try:
        regime = api("/api/market-regime", 30)
        regime_line = f"{regime.get('label', '?')} — {regime.get('note', '')}"
    except Exception:
        regime_line = "(無法判斷)"

    # 2. 跑全策略
    results = []
    for s in STRATEGIES:
        try:
            params = f"strategy={s}&start={start}&end={end}"
            for k, v in PARAMS.items():
                params += f"&{k}={v}"
            d = api(f"/api/backtest?{params}", timeout=180)
            x = d["summary"]
            alpha = x.get("alpha") or 0
            ret   = x.get("total_return") or 0
            trades = x.get("n_trades") or 0
            win   = x.get("win_rate") or 0
            dd    = x.get("max_drawdown") or 0
            results.append({
                "strategy": s, "alpha": alpha, "return": ret,
                "win": win, "trades": trades, "dd": dd,
                "status": classify(alpha, trades),
            })
        except Exception as e:
            results.append({
                "strategy": s, "alpha": 0, "trades": 0,
                "status": "error", "err": str(e),
            })

    # 3. 排序
    results.sort(key=lambda x: -x.get("alpha", 0))

    # 4. 組訊息
    lines = [
        f"🩺 *策略體檢 — {now.strftime('%Y/%m')}*",
        "",
        f"🌐 regime: {regime_line}",
        f"📅 區間: 近 {LOOKBACK_MONTHS} 個月 ({start} ~ {end}, hold=10, n=3)",
        "",
        "✅ *活著 (alpha > +5):*",
    ]
    alive = [r for r in results if r["status"] == "alive"]
    for r in alive:
        lines.append(f"  ✅ {r['strategy']:<16s} ret {r['return']:+5.1f}% / α{r['alpha']:+5.1f} / win {r['win']:.0f}% / DD {r['dd']:.0f}%")
    if not alive:
        lines.append("  (無策略存活)")

    marginal = [r for r in results if r["status"] == "marginal"]
    if marginal:
        lines.append("")
        lines.append("⚠️ *邊緣 (低 alpha 或 trades < 5):*")
        for r in marginal:
            lines.append(f"  ⚠️ {r['strategy']:<16s} α{r['alpha']:+5.1f} / trades {r['trades']}")

    dead = [r for r in results if r["status"] in ("dead", "dead_no_trades")]
    if dead:
        lines.append("")
        lines.append("❌ *淘汰 (近期無 alpha):*")
        for r in dead:
            reason = "0 trades 無觸發" if r["status"] == "dead_no_trades" else f"α{r['alpha']:+.1f}"
            lines.append(f"  ❌ {r['strategy']:<16s} {reason}")

    err = [r for r in results if r["status"] == "error"]
    if err:
        lines.append("")
        for r in err:
            lines.append(f"  💥 {r['strategy']} 跑掛: {r.get('err', '')[:80]}")

    # 5. 推薦
    lines.append("")
    if alive:
        top = alive[0]
        lines.append(f"🎯 *本月推薦: {top['strategy']}*")
        lines.append(f"   alpha +{top['alpha']:.1f} / 勝率 {top['win']:.0f}% / DD {top['dd']:.0f}%")
        # 如果 score_adaptive 在前 3 名且差距 < 5,推薦它(自動換 regime)
        adaptive = next((r for r in alive[:3] if r["strategy"] == "score_adaptive"), None)
        if adaptive and adaptive["strategy"] != top["strategy"] and (top["alpha"] - adaptive["alpha"]) < 5:
            lines.append(f"   或 score_adaptive (α+{adaptive['alpha']:.1f}) — regime 切換時自動換權重")
    else:
        lines.append("⚠️ *本月無策略存活,建議空手或減碼*")

    lines.append("")
    lines.append(f"_由 strategy_health_check.py {now.strftime('%H:%M')} 自動產生_")

    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:3900] + "\n\n_…內容過長已截斷_"

    ok = send_telegram(body)
    print(f"[health_check] sent={ok}, length={len(body)}")


if __name__ == "__main__":
    main()
