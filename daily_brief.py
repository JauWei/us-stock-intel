"""
每日早報 — 美股情報站

執行: python daily_brief.py
排程: Task Scheduler 每天 8:00 AM 觸發
功能:
  1. 總經快照 (VIX/10Y/DXY/2Y) + 燈號
  2. 今日 / 3 天內 watchlist 財報日
  3. 昨日異動大於 3% 的持股
  4. 觸發中的 alerts (RSI/訊號)

需先設定 telegram.json (bot_token + chat_id),與 server.py 同目錄。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

ROOT = Path(__file__).resolve().parent
TELE_FILE = ROOT / "telegram.json"
SERVER_URL = os.environ.get("US_INTEL_URL", "http://localhost:18506")
TIMEOUT = 30


def ensure_server_running(max_wait: int = 60) -> bool:
    """確保 server.py 已啟動。沒有就在背景啟動並等就緒。"""
    try:
        r = requests.get(f"{SERVER_URL}/api/stocks", timeout=5)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    server_py = ROOT / "server.py"
    if not server_py.exists():
        return False
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    py_exe = str(pythonw) if pythonw.exists() else sys.executable
    try:
        subprocess.Popen(
            [py_exe, str(server_py)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                          getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        print(f"[ensure_server] 已啟動 server.py, 等待 ready…")
    except Exception as e:
        print(f"[ensure_server] 啟動失敗: {e}")
        return False
    for i in range(max_wait):
        time.sleep(1)
        try:
            r = requests.get(f"{SERVER_URL}/api/stocks", timeout=3)
            if r.status_code == 200:
                print(f"[ensure_server] server 在 {i+1}s 後就緒")
                return True
        except Exception:
            continue
    return False


def load_telegram():
    try:
        with open(TELE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def send_telegram(text: str) -> bool:
    cfg = load_telegram()
    token = cfg.get("bot_token", "")
    chat = cfg.get("chat_id", "")
    if not token or not chat:
        print("[daily_brief] telegram.json 未設定")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id":    chat,
            "text":       text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[daily_brief] telegram fail: {e}")
        return False


def api(path: str, timeout: int = TIMEOUT):
    r = requests.get(f"{SERVER_URL}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def section_macro() -> str:
    """總經區塊"""
    try:
        macro = api("/api/macro", 20)
    except Exception as e:
        return f"🌍 *總經* — 無法取得 ({e})\n"

    icons = {"danger": "🔴", "calm": "🟢", "high": "🟠", "low": "🔵",
             "strong": "🟠", "weak": "🔵", "neutral": "⚪"}
    lines = ["🌍 *總經背景*"]
    flags = []
    for m in macro:
        if m.get("value") is None:
            continue
        ic = icons.get(m.get("status", "neutral"), "⚪")
        chg = m.get("change_pct", 0)
        arrow = "▲" if chg > 0 else "▼" if chg < 0 else "·"
        chg_str = f" {arrow}{abs(chg):.1f}%"
        lines.append(f"  {ic} *{m['code']}*: {m['value']:.2f}{m['unit']}{chg_str}")
        # 警示
        if m["code"] == "VIX" and m.get("status") == "danger":
            flags.append("⚠️ VIX 過高,風險偏好降溫")
        elif m["code"] == "10Y" and m.get("status") == "high":
            flags.append("⚠️ 10Y > 4.5%,成長股壓力大")
    if flags:
        lines.append("")
        lines.extend(flags)
    return "\n".join(lines) + "\n"


def section_earnings() -> str:
    """財報日曆 (今日 + 7 天內)"""
    try:
        ec = api("/api/earnings-calendar?days=7", 60)
    except Exception as e:
        return f"📅 *財報* — 無法取得 ({e})\n"

    soon = [r for r in ec if r.get("days_to") is not None and 0 <= r["days_to"] <= 7]
    if not soon:
        return "📅 *未來 7 天無 watchlist 財報*\n"
    lines = ["📅 *未來 7 天財報*"]
    for r in soon[:10]:
        d = r["days_to"]
        marker = "🔔 *TODAY*" if d == 0 else (f"+{d}d" if d > 0 else f"{d}d")
        hist = ""
        if r.get("history"):
            last = r["history"][0]
            sp = last.get("surprise_pct")
            if sp is not None:
                ic = "📈" if sp >= 0 else "📉"
                hist = f" (上季 {ic}{sp:+.1f}%)"
        lines.append(f"  {marker} *{r['code']}* {r.get('name', '')} — {r.get('earnings_date', '?')}{hist}")
    return "\n".join(lines) + "\n"


def section_overnight_movers() -> str:
    """昨晚異動 >3% 的 watchlist 個股"""
    try:
        stocks = api("/api/stocks", 30)
    except Exception as e:
        return f"📊 *異動* — 無法取得 ({e})\n"

    movers = []
    for s in stocks:
        prev = s.get("prev") or 0
        if not prev:
            continue
        chg = (s["price"] - prev) / prev * 100
        if abs(chg) >= 3:
            movers.append((chg, s))
    movers.sort(key=lambda x: -abs(x[0]))
    if not movers:
        return "📊 *昨日異動* — 無 ±3% 以上個股\n"

    lines = ["📊 *昨日異動 (±3%)*"]
    for chg, s in movers[:8]:
        ic = "🚀" if chg > 0 else "⚠️"
        sigs = ""
        if s.get("signals"):
            sigs = " · " + "/".join(x.get("label", "") for x in s["signals"][:2])
        lines.append(f"  {ic} *{s['code']}* {chg:+.2f}% (${s['price']:.2f}){sigs}")
    return "\n".join(lines) + "\n"


def section_squeeze() -> str:
    """軋空 Top 3 (高分才推)"""
    try:
        sq = api("/api/short-squeeze", 60)
    except Exception:
        return ""

    top = [s for s in sq if s.get("score", 0) >= 40][:3]
    if not top:
        return ""
    lines = ["🔥 *軋空候選 Top 3*"]
    for s in top:
        dtc = s.get("short_ratio")
        dtcStr = f"DTC {dtc:.1f}d" if dtc else "—"
        lines.append(f"  *{s['code']}* ({s['score']} 分) — {dtcStr}, 今日 {s.get('change_pct', 0):+.2f}%")
    return "\n".join(lines) + "\n"


def main():
    if not load_telegram():
        print("[daily_brief] telegram.json 沒設,放棄")
        sys.exit(0)
    if not ensure_server_running():
        send_telegram("⚠️ *美股早報失敗* — 無法啟動 server,請檢查")
        sys.exit(1)

    now = datetime.now()
    header = f"☀️ *美股早報 — {now.strftime('%m/%d %A')}*\n"
    body = "\n".join([
        header,
        section_macro(),
        section_earnings(),
        section_overnight_movers(),
        section_squeeze(),
        f"_由 daily_brief.py {now.strftime('%H:%M')} 自動產生_",
    ])

    # Telegram 4096 char limit
    if len(body) > 4000:
        body = body[:3900] + "\n\n_…內容過長已截斷_"

    ok = send_telegram(body)
    print(f"[daily_brief] sent={ok}, length={len(body)}")


if __name__ == "__main__":
    main()
