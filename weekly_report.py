"""每週週報 — 美股情報站

執行: python weekly_report.py
排程: Task Scheduler 每週日 18:00 觸發

內容:
  1. 一週主題輪動 Top 3 + Bottom 3 (動量)
  2. watchlist 過去一週漲跌 Top 5 / Bottom 5
  3. 持倉週表現 (持股、加權報酬、最強/最弱)
  4. 下週 holdings 財報日
  5. 過去 7 天觸發的警示統計

需先設定 telegram.json (與 daily_brief.py 同檔)。
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from datetime import datetime

import requests

ROOT = Path(__file__).resolve().parent
TELE_FILE = ROOT / "telegram.json"
SERVER_URL = os.environ.get("US_INTEL_URL", "http://localhost:18506")
TIMEOUT = 60


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
        print("[weekly_report] telegram.json 未設定")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[weekly_report] tg fail: {e}")
        return False


def api(path: str, timeout: int = TIMEOUT):
    r = requests.get(f"{SERVER_URL}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def section_themes():
    try:
        themes = api("/api/theme-rotation", 60)
    except Exception as e:
        return f"🎨 *主題輪動* — 失敗 ({e})\n"
    if not themes:
        return "🎨 *主題輪動* — 無資料\n"
    top = sorted(themes, key=lambda x: -x.get("ret_1w", 0))[:3]
    bot = sorted(themes, key=lambda x: x.get("ret_1w", 0))[:3]
    lines = ["🎨 *本週主題輪動*", "  📈 *最強*:"]
    for t in top:
        lines.append(f"    +{t['ret_1w']:.2f}% — *{t['theme']}* ({t['n']} 檔)")
    lines.append("  📉 *最弱*:")
    for t in bot:
        lines.append(f"    {t['ret_1w']:+.2f}% — *{t['theme']}* ({t['n']} 檔)")
    return "\n".join(lines) + "\n"


def section_movers():
    try:
        stocks = api("/api/stocks", 30)
    except Exception as e:
        return f"📊 *漲跌榜* — 失敗 ({e})\n"
    movers = []
    for s in stocks:
        prev = s.get("prev") or 0
        if not prev: continue
        chg = (s["price"] - prev) / prev * 100
        movers.append((chg, s))
    movers.sort(key=lambda x: -x[0])
    top, bot = movers[:5], movers[-5:][::-1]
    lines = ["", "📊 *本週漲跌榜 (vs 昨收;近似週度)*", "  🚀 *Top 5*:"]
    for chg, s in top:
        lines.append(f"    *{s['code']}* {chg:+.2f}% — {s['name']} (${s['price']:.2f})")
    lines.append("  ⚠️ *Bottom 5*:")
    for chg, s in bot:
        lines.append(f"    *{s['code']}* {chg:+.2f}% — {s['name']} (${s['price']:.2f})")
    return "\n".join(lines) + "\n"


def section_portfolio():
    try:
        pf = api("/api/portfolio", 30)
    except Exception as e:
        return f"💼 *持倉* — 失敗 ({e})\n"
    holdings = pf.get("holdings", [])
    summary  = pf.get("summary", {})
    if not holdings:
        return "💼 *持倉* — 無資料\n"
    lines = ["", "💼 *持倉週表現*"]
    pnl = summary.get("total_pnl", 0)
    pnl_pct = summary.get("total_pnl_pct", 0)
    val = summary.get("total_value", 0)
    lines.append(f"  總市值: *${val:,.0f}* · 未實現: *{pnl:+,.0f} ({pnl_pct:+.2f}%)*")
    # Top 5 by pnl_pct
    sorted_h = sorted(holdings, key=lambda x: -x.get("pnl_pct", 0))
    if len(sorted_h) >= 2:
        best, worst = sorted_h[0], sorted_h[-1]
        lines.append(f"  💎 *最強*: {best['code']} {best.get('pnl_pct', 0):+.2f}%")
        lines.append(f"  📉 *最弱*: {worst['code']} {worst.get('pnl_pct', 0):+.2f}%")
    return "\n".join(lines) + "\n"


def section_earnings():
    try:
        ec = api("/api/earnings-calendar?days=10", 60)
    except Exception as e:
        return ""
    soon = [r for r in ec if r.get("days_to") is not None and 0 <= r["days_to"] <= 10]
    if not soon:
        return ""
    lines = ["", "📅 *下週 (10 天內) 財報*"]
    for r in soon[:8]:
        d = r.get("days_to")
        lines.append(f"  +{d}d *{r['code']}* — {r.get('earnings_date', '?')}")
    return "\n".join(lines) + "\n"


def section_alerts_review():
    try:
        log = api("/api/alerts-log?days=7", 60)
    except Exception:
        return ""
    entries = log.get("entries", [])
    stats   = log.get("stats", {})
    if not entries:
        return ""
    lines = ["", f"📜 *本週警示觸發 {len(entries)} 次*"]
    for k, st in stats.items():
        wr = st.get("win_rate_5d")
        avg = st.get("avg_5d")
        if wr is None: continue
        lines.append(f"  *{k}*: {st['n']} 次 · 5d 勝率 {wr}% · 平均 {avg:+.2f}%")
    return "\n".join(lines) + "\n"


def main():
    if not load_telegram():
        print("[weekly_report] telegram.json 沒設,放棄")
        sys.exit(0)
    now = datetime.now()
    header = f"📈 *美股週報 — {now.strftime('%Y/%m/%d (%A)')}*\n"
    body = "\n".join([
        header,
        section_themes(),
        section_movers(),
        section_portfolio(),
        section_earnings(),
        section_alerts_review(),
        f"_由 weekly_report.py {now.strftime('%H:%M')} 自動產生_",
    ])
    if len(body) > 4000:
        body = body[:3900] + "\n\n_…內容過長已截斷_"
    ok = send_telegram(body)
    print(f"[weekly_report] sent={ok}, length={len(body)}")


if __name__ == "__main__":
    main()
