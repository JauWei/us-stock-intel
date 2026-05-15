"""
美股情報站 backend - FastAPI + yfinance + Gemini + Telegram
啟動: python server.py  →  http://localhost:18506/

功能:
- 即時價格 / OHLC / 技術指標 (yfinance)
- 分析師評等 (yfinance.recommendations) 取代台股的三大法人
- 持久化觀察清單 (watchlist.json)
- 訊號偵測 (黃金/死亡交叉、KD 超買賣、突破 20 日新高低)
- 多週期 K 線 (日/週/月)
- 英文新聞 + Gemini 自動翻成繁體中文
- Telegram 警示 (每 5 分鐘背景掃描)
- 熱度榜 / 族群分組
- 季營收 + 季 EPS (yfinance quarterly_income_stmt)
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
WATCHLIST_FILE = ROOT / "watchlist.json"
ALERTS_FILE    = ROOT / "alerts.json"
TELEGRAM_FILE  = ROOT / "telegram.json"
PORTFOLIO_FILE = ROOT / "portfolio.json"
GEMINI_FILE    = ROOT / "gemini.json"

# ----------------------------------------------------------------------------
# Default watchlist (首次啟動時寫入 watchlist.json)
# ----------------------------------------------------------------------------
DEFAULT_WATCHLIST: dict[str, dict[str, str]] = {
    # === AI 基礎設施 (Hardware Layer) ===
    # GPU / 加速器
    "NVDA":  {"name": "NVIDIA",       "tag": "GPU · AI 訓練 / 推論",       "yf": "NVDA",  "group": "GPU / 加速器"},
    "AMD":   {"name": "AMD",          "tag": "GPU / CPU · MI300X",         "yf": "AMD",   "group": "GPU / 加速器"},

    # 網路晶片 / Fabric
    "AVGO":  {"name": "Broadcom",     "tag": "網通 · ASIC / 交換器",       "yf": "AVGO",  "group": "網路 Fabric"},
    "MRVL":  {"name": "Marvell",      "tag": "資料中心 · DPU / 互連",      "yf": "MRVL",  "group": "網路 Fabric"},
    "ANET":  {"name": "Arista",       "tag": "高速交換器 · 雲端網路",      "yf": "ANET",  "group": "網路 Fabric"},

    # 伺服器組裝
    "SMCI":  {"name": "Super Micro",  "tag": "AI 伺服器 / 液冷整合",       "yf": "SMCI",  "group": "伺服器組裝"},

    # 電力 / 散熱 (AI 缺電題材)
    "VRT":   {"name": "Vertiv",       "tag": "資料中心散熱 / UPS",         "yf": "VRT",   "group": "電力 / 散熱"},
    "CEG":   {"name": "Constellation","tag": "核能電力 · AI 缺電題材",     "yf": "CEG",   "group": "電力 / 散熱"},

    # Hyperscalers (買家層 / 資本支出端)
    "MSFT":  {"name": "Microsoft",    "tag": "雲端 Azure / Copilot",        "yf": "MSFT",  "group": "Hyperscalers"},
    "GOOGL": {"name": "Alphabet",     "tag": "搜尋 / 廣告 / GCP",           "yf": "GOOGL", "group": "Hyperscalers"},
    "AMZN":  {"name": "Amazon",       "tag": "AWS / 電商",                  "yf": "AMZN",  "group": "Hyperscalers"},
    "META":  {"name": "Meta",         "tag": "社群 / Llama / Reality Labs", "yf": "META",  "group": "Hyperscalers"},

    # === 半導體循環 (Semiconductor Cycles) ===
    # 半導體設備 (WFE)
    "ASML":  {"name": "ASML",         "tag": "微影設備 · EUV 壟斷",        "yf": "ASML",  "group": "半導體設備"},
    "AMAT":  {"name": "Applied Mat.", "tag": "半導體設備 · 蝕刻沉積",      "yf": "AMAT",  "group": "半導體設備"},

    # 晶圓代工
    "TSM":   {"name": "TSMC ADR",     "tag": "晶圓代工 · 全球領導",        "yf": "TSM",   "group": "晶圓代工"},

    # IC 設計
    "QCOM":  {"name": "Qualcomm",     "tag": "手機 SoC / 5G",               "yf": "QCOM",  "group": "IC 設計"},
    "ARM":   {"name": "Arm Holdings", "tag": "IP / 矽智財",                 "yf": "ARM",   "group": "IC 設計"},

    # 記憶體
    "MU":    {"name": "Micron",       "tag": "記憶體 · DRAM / HBM",         "yf": "MU",    "group": "記憶體"},

    # === 軟體 (Enterprise Software) ===
    "PLTR":  {"name": "Palantir",     "tag": "AI SaaS / 數據分析",         "yf": "PLTR",  "group": "雲端 / SaaS"},
    "CRM":   {"name": "Salesforce",   "tag": "雲端 CRM / Agentforce",      "yf": "CRM",   "group": "雲端 / SaaS"},
    "CRWD":  {"name": "CrowdStrike",  "tag": "資訊安全 · 端點防護",        "yf": "CRWD",  "group": "資訊安全"},

    # === EV / 其他 ===
    "AAPL":  {"name": "Apple",        "tag": "消費電子 · iPhone",          "yf": "AAPL",  "group": "消費電子"},
    "TSLA":  {"name": "Tesla",        "tag": "EV / 自駕 / Robotaxi",       "yf": "TSLA",  "group": "EV / 自駕"},
    "NFLX":  {"name": "Netflix",      "tag": "流媒體",                      "yf": "NFLX",  "group": "其他"},
    "JPM":   {"name": "JPMorgan",     "tag": "金融 · 銀行",                "yf": "JPM",   "group": "其他"},
}

CACHE_TTL = 300
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def cache_get(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def cache_set(key: str, val: Any) -> None:
    _cache[key] = (time.time(), val)


# ----------------------------------------------------------------------------
# 持久化
# ----------------------------------------------------------------------------
def load_json(p: Path, default: Any) -> Any:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(p: Path, data: Any) -> None:
    with _lock:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _migrate_watchlist_groups(wl: dict) -> dict:
    """套用 DEFAULT_WATCHLIST 的最新族群分類到既有清單。
    規則：
    - 既有代號若在 DEFAULT 內，更新 group / tag 為新版（族群細分）
    - 既有代號不在 DEFAULT 內（user 自行加的，例如「持股」群組）→ 保留不動
    - DEFAULT 有但既有清單沒有 → 不自動加入
    """
    changed = False
    for code, default_meta in DEFAULT_WATCHLIST.items():
        if code in wl:
            cur = wl[code]
            if cur.get("group") != default_meta["group"] or cur.get("tag") != default_meta["tag"]:
                cur["group"] = default_meta["group"]
                cur["tag"]   = default_meta["tag"]
                if not cur.get("name"):
                    cur["name"] = default_meta["name"]
                changed = True
    if changed:
        save_json(WATCHLIST_FILE, wl)
        print(f"[migrate] watchlist 族群分類已套用最新版")
    return wl


def load_watchlist() -> dict:
    wl = load_json(WATCHLIST_FILE, None)
    if wl is None:
        save_json(WATCHLIST_FILE, DEFAULT_WATCHLIST)
        return DEFAULT_WATCHLIST.copy()
    return _migrate_watchlist_groups(wl)


def load_alerts() -> dict:
    return load_json(ALERTS_FILE, {})


def load_telegram() -> dict:
    return load_json(TELEGRAM_FILE, {"bot_token": "", "chat_id": ""})


# ----------------------------------------------------------------------------
# 技術指標
# ----------------------------------------------------------------------------
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi_indicator(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def macd_indicator(s: pd.Series):
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def kd_indicator(df: pd.DataFrame, n: int = 9):
    low_n = df["Low"].rolling(n).min()
    high_n = df["High"].rolling(n).max()
    rsv = (df["Close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return k, d


def safe(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        if np.isnan(v) or np.isinf(v):
            return None
        return float(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    return v


# ----------------------------------------------------------------------------
# 訊號偵測
# ----------------------------------------------------------------------------
def detect_signals(closes: pd.Series, ma5_s: pd.Series, ma20_s: pd.Series,
                   k_s: pd.Series, d_s: pd.Series, rsi_s: pd.Series,
                   highs: pd.Series, lows: pd.Series, period: str = "D") -> list[dict]:
    sigs: list[dict] = []
    # 各週期的 breakout 視窗與標籤
    breakout_cfg = {
        "D": (20, "20 日"),
        "W": (20, "20 週"),
        "M": (12, "12 月"),
    }
    bk_n, bk_label = breakout_cfg.get(period, breakout_cfg["D"])

    if len(closes) < max(21, bk_n + 1):
        return sigs

    # 均線黃金 / 死亡交叉 (MA5 vs MA20)
    if not pd.isna(ma5_s.iloc[-1]) and not pd.isna(ma20_s.iloc[-2]):
        if ma5_s.iloc[-2] <= ma20_s.iloc[-2] and ma5_s.iloc[-1] > ma20_s.iloc[-1]:
            sigs.append({"key": "golden_cross", "label": "🌟黃金交叉", "color": "red"})
        elif ma5_s.iloc[-2] >= ma20_s.iloc[-2] and ma5_s.iloc[-1] < ma20_s.iloc[-1]:
            sigs.append({"key": "death_cross", "label": "💀死亡交叉", "color": "green"})

    # KD 黃金 / 死亡交叉
    if len(k_s) >= 2:
        last_k, prev_k = float(k_s.iloc[-1]), float(k_s.iloc[-2])
        last_d, prev_d = float(d_s.iloc[-1]), float(d_s.iloc[-2])
        if prev_k <= prev_d and last_k > last_d and last_k < 50:
            sigs.append({"key": "kd_cross_up", "label": "🔼KD 黃金交叉", "color": "red"})
        elif prev_k >= prev_d and last_k < last_d and last_k > 50:
            sigs.append({"key": "kd_cross_dn", "label": "🔽KD 死亡交叉", "color": "green"})
        if last_k > 80:
            sigs.append({"key": "kd_overbought", "label": "⚠️KD 超買", "color": "orange"})
        elif last_k < 20:
            sigs.append({"key": "kd_oversold", "label": "🚀KD 超賣", "color": "cyan"})

    # RSI 超買 / 超賣
    if not pd.isna(rsi_s.iloc[-1]):
        rsi_v = float(rsi_s.iloc[-1])
        if rsi_v > 75:
            sigs.append({"key": "rsi_overbought", "label": "🔥RSI 超買", "color": "orange"})
        elif rsi_v < 25:
            sigs.append({"key": "rsi_oversold", "label": "❄️RSI 超賣", "color": "cyan"})

    # 突破新高 / 跌破新低
    price = float(closes.iloc[-1])
    high_n = float(highs.iloc[-(bk_n+1):-1].max())
    low_n  = float(lows.iloc[-(bk_n+1):-1].min())
    if price > high_n * 1.001:
        sigs.append({"key": "breakout_high", "label": f"🚀 突破 {bk_label}新高", "color": "red"})
    if price < low_n * 0.999:
        sigs.append({"key": "breakdown_low", "label": f"📉 跌破 {bk_label}新低", "color": "green"})

    return sigs


# ----------------------------------------------------------------------------
# 分析師評等 (yfinance.recommendations) — 取代台股版的三大法人
# ----------------------------------------------------------------------------
def fetch_institutional(yf_code: str, days: int = 10) -> dict | None:
    """回傳近 N 個月分析師評等變化。
    沿用台股版 institutional 資料形狀讓前端不必改：
        fi (外資)   → strongBuy
        it (投信)   → buy
        dealer (自營商) → hold
    使用者一看顏色即可分辨。負面評等(sell/strongSell) 加總後以 negative 一個欄位回傳。
    """
    cached = cache_get(f"inst:{yf_code}")
    if cached:
        return cached
    try:
        rec = yf.Ticker(yf_code).recommendations
    except Exception as e:
        print(f"[rec] {yf_code}: {e}")
        return None
    if rec is None or len(rec) == 0:
        return None

    df = rec.copy()
    if "period" in df.columns:
        df = df.set_index("period")
    df = df.sort_index().tail(days)
    if df.empty:
        return None

    # 期間 label: 0m / -1m / -2m...
    labels = []
    for idx in df.index:
        s = str(idx)
        labels.append(s if s.startswith("-") or s == "0m" else s)

    sb = df.get("strongBuy", pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    b  = df.get("buy",        pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    h  = df.get("hold",       pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    sl = df.get("sell",       pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    ssl= df.get("strongSell", pd.Series([0] * len(df))).fillna(0).astype(int).tolist()
    negative = [-(s + ss) for s, ss in zip(sl, ssl)]  # 正負標示

    out = {
        "dates":  labels,
        "fi":     sb,        # strongBuy → 對齊「外資」位置
        "it":     b,         # buy
        "dealer": h,         # hold
        "neg":    negative,  # sell + strongSell 合併（顯示為負值）
        "_legend": {"fi": "Strong Buy", "it": "Buy", "dealer": "Hold", "neg": "Sell"},
    }
    cache_set(f"inst:{yf_code}", out)
    return out


# ----------------------------------------------------------------------------
# 多週期 K 線資料抓取
# ----------------------------------------------------------------------------
PERIOD_CFG = {
    "D": {"period": "1y",  "interval": "1d",  "n": 80,  "label": "日"},
    "W": {"period": "3y",  "interval": "1wk", "n": 80,  "label": "週"},
    "M": {"period": "10y", "interval": "1mo", "n": 80,  "label": "月"},
}


def fetch_stock(code: str, period: str = "D", force: bool = False) -> dict:
    period = period.upper() if period.upper() in PERIOD_CFG else "D"
    cache_key = f"stock:{code}:{period}"
    if not force:
        cached = cache_get(cache_key)
        if cached:
            return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404, f"未追蹤股票 {code}")

    cfg = PERIOD_CFG[period]
    ticker = yf.Ticker(info["yf"])
    hist = ticker.history(period=cfg["period"], interval=cfg["interval"], auto_adjust=False)
    if hist.empty:
        raise HTTPException(503, f"yfinance 取不到 {code} ({info['yf']}) 資料")
    hist = hist.dropna(subset=["Close"])

    closes = hist["Close"]
    ma5_s, ma20_s, ma60_s = sma(closes, 5), sma(closes, 20), sma(closes, 60)
    rsi_s = rsi_indicator(closes, 14)
    dif_s, dea_s, macd_s = macd_indicator(closes)
    k_s, d_s = kd_indicator(hist, 9)

    last = hist.iloc[-1]
    prev_close = float(hist.iloc[-2]["Close"]) if len(hist) > 1 else float(last["Close"])
    N = cfg["n"]
    recent = hist.iloc[-N:]

    klines = [
        {
            "date": idx.strftime("%m/%d") if period == "D" else idx.strftime("%Y/%m" if period == "M" else "%m/%d"),
            "ohlc": [
                round(float(r["Open"]),  2),
                round(float(r["Close"]), 2),
                round(float(r["Low"]),   2),
                round(float(r["High"]),  2),
            ],
            "volume": int(r["Volume"]) if r["Volume"] else 0,    # 美股: shares (整數)
        }
        for idx, r in recent.iterrows()
    ]

    def slice_series(s: pd.Series) -> list:
        return [safe(v) for v in s.iloc[-N:].tolist()]

    price = float(last["Close"])
    ma5_v  = safe(ma5_s.iloc[-1])  or price
    ma20_v = safe(ma20_s.iloc[-1]) or price
    ma60_v = safe(ma60_s.iloc[-1]) or price

    if ma5_v > ma20_v > ma60_v:
        ma_status = "多頭排列"
    elif ma5_v < ma20_v < ma60_v:
        ma_status = "空頭排列"
    else:
        ma_status = "盤整"

    if price > ma20_v and price > ma60_v:
        trend = "多頭趨勢"
    elif price < ma20_v and price < ma60_v:
        trend = "空頭趨勢"
    else:
        trend = "盤整"

    # 美股 volume 單位為 shares，不像台股要除以 1000
    avg_vol_5 = float(hist["Volume"].iloc[-5:].mean())
    today_vol = float(last["Volume"])
    vol_change = (today_vol - avg_vol_5) / avg_vol_5 * 100 if avg_vol_5 > 0 else 0.0

    rsi_v = safe(rsi_s.iloc[-1]) or 50.0
    bias = (price - ma20_v) / ma20_v * 100 if ma20_v else 0

    if rsi_v > 75 and bias > 10:
        risk = "high"
        risk_note = f"RSI {rsi_v:.1f} 超買 + 乖離 {bias:.1f}%，主力疑似出貨。"
        risk_banner = f"高檔過熱警示：RSI {rsi_v:.1f} + 乖離 {bias:.1f}%"
    elif rsi_v > 70 or bias > 8:
        risk = "mid"
        risk_note = f"短線過熱（RSI {rsi_v:.1f}），等待回測。"
        risk_banner = "中度觀察：技術指標過熱"
    elif rsi_v < 30:
        risk = "mid"
        risk_note = f"RSI {rsi_v:.1f} 超賣，可分批承接。"
        risk_banner = "短線超賣，留意止穩"
    else:
        risk = "low"
        risk_note = "技術面健康，籌碼穩定。"
        risk_banner = "趨勢明確 · 操作偏中性"

    span = float(last["High"]) - float(last["Low"])
    if span <= 0:
        outer_pct = 0.5
    else:
        outer_pct = max(0.3, min(0.7, (float(last["Close"]) - float(last["Low"])) / span))
    outer_v = int(round(today_vol * outer_pct))
    inner_v = max(0, int(round(today_vol)) - outer_v)

    high60 = float(recent["High"].max())
    low60  = float(recent["Low"].min())
    if price > ma20_v:
        supports = sorted({int(round(ma20_v)), int(round(ma60_v))})
        resists = sorted({int(round(min(high60, price * 1.05))), int(round(high60))})
    else:
        supports = sorted({int(round(low60)), int(round(ma60_v))})
        resists = sorted({int(round(ma20_v)), int(round(ma5_v))})

    atr = float((hist["High"] - hist["Low"]).iloc[-14:].mean())
    swing = atr if atr > 0 else price * 0.015
    scenario = {
        "up":   {"entry": f"{price:.0f} ~ {price + swing*0.5:.0f}",
                 "sl":    int(round(price - swing * 1.0)),
                 "tp":    f"{int(round(price + swing*2))} / {int(round(price + swing*4))}"},
        "flat": {"entry": f"{price - swing*0.7:.0f} ~ {price - swing*0.2:.0f}",
                 "sl":    int(round(price - swing * 1.5)),
                 "tp":    f"{int(round(price + swing*1))} / {int(round(price + swing*2.5))}"},
        "down": {"entry": f"{price - swing*2:.0f} ~ {price - swing*1.2:.0f}",
                 "sl":    int(round(price - swing * 3)),
                 "tp":    f"{int(round(price - swing*0.3))} / {int(round(price + swing*1))}"},
    }

    inst = fetch_institutional(info["yf"]) or {"dates": [], "fi": [], "it": [], "dealer": [], "neg": []}
    fi_today = inst["fi"][-1] if inst["fi"] else 0      # Strong Buy
    it_today = inst["it"][-1] if inst["it"] else 0      # Buy
    dealer_today = inst["dealer"][-1] if inst["dealer"] else 0  # Hold
    neg_today = abs(inst.get("neg", [0])[-1]) if inst.get("neg") else 0  # Sell + StrongSell
    fi_5  = sum(inst["fi"][-5:])  if inst["fi"] else 0
    fi_10 = sum(inst["fi"])       if inst["fi"] else 0
    it_10 = sum(inst["it"])       if inst["it"] else 0

    bullish = fi_today + it_today
    bearish = neg_today
    if bullish > bearish * 2 and bullish >= 5:
        chip_note = f"分析師偏多：Strong Buy {fi_today} + Buy {it_today} 明顯多於 Sell {neg_today}。"
    elif bearish > bullish:
        chip_note = f"分析師偏空：Sell {neg_today} 多於買進評等 ({bullish})。"
    else:
        chip_note = f"分析師中性：Buy {bullish} / Hold {dealer_today} / Sell {bearish}。"

    # 訊號（日/週/月皆計算，自動換 lookback 視窗）
    signals = detect_signals(closes, ma5_s, ma20_s, k_s, d_s, rsi_s, hist["High"], hist["Low"], period=period)

    out = {
        "code":   code,
        "name":   info["name"],
        "tag":    info["tag"],
        "group":  info.get("group", "自選"),
        "period": period,
        "price":   round(price, 2),
        "prev":    round(prev_close, 2),
        "open":    round(float(last["Open"]),  2),
        "high":    round(float(last["High"]),  2),
        "low":     round(float(last["Low"]),   2),
        "volume":  int(round(today_vol)),
        "avgVol":  int(round(avg_vol_5)),
        "inner":   inner_v,
        "outer":   outer_v,
        "ma5":  round(ma5_v, 2),
        "ma20": round(ma20_v, 2),
        "ma60": round(ma60_v, 2),
        "rsi":   round(rsi_v, 2),
        "kd_k":  round(safe(k_s.iloc[-1])  or 0, 2),
        "kd_d":  round(safe(d_s.iloc[-1])  or 0, 2),
        "dif":   round(safe(dif_s.iloc[-1]) or 0, 2),
        "dea":   round(safe(dea_s.iloc[-1]) or 0, 2),
        "macd":  round(safe(macd_s.iloc[-1]) or 0, 2),
        "volChange": round(vol_change, 1),
        "trend":     trend,
        "maStatus":  ma_status,
        "risk":       risk,
        "riskNote":   risk_note,
        "riskBanner": risk_banner,
        "resist":  resists,
        "support": supports,
        "klines":      klines,
        "ma5_series":  slice_series(ma5_s),
        "ma20_series": slice_series(ma20_s),
        "ma60_series": slice_series(ma60_s),
        "scenario": scenario,
        "institutional": inst,
        "chip": {
            "fi_today": fi_today, "it_today": it_today, "dealer_today": dealer_today,
            "fi_5": fi_5, "fi_10": fi_10, "it_10": it_10,
            "conclusion": chip_note,
        },
        "signals": signals,
        "asOf":    str(hist.index[-1].date()),
        "source":  "live",
    }
    cache_set(cache_key, out)

    # 順便檢查警示
    if period == "D":
        try:
            check_alert(code, price, prev_close, info["name"])
        except Exception as e:
            print(f"[alert check] {code}: {e}")

    return out


# ----------------------------------------------------------------------------
# 新聞 (yfinance 英文 + Gemini 自動翻譯成繁體中文)
# ----------------------------------------------------------------------------
import re as _re


def _fetch_yfinance_news(yf_code: str) -> list:
    try:
        raw = yf.Ticker(yf_code).news or []
        out = []
        for n in raw[:12]:
            content = n.get("content") or n
            title = content.get("title") or n.get("title") or ""
            publisher = (
                (content.get("provider") or {}).get("displayName")
                or n.get("publisher") or ""
            )
            link = (
                (content.get("clickThroughUrl") or {}).get("url")
                or (content.get("canonicalUrl") or {}).get("url")
                or n.get("link") or ""
            )
            ts = n.get("providerPublishTime") or 0
            pub_date = content.get("pubDate", "")
            if not ts and pub_date:
                try:
                    ts = int(pd.Timestamp(pub_date).timestamp())
                except Exception:
                    ts = 0
            out.append({"title": title, "publisher": publisher, "link": link, "time": ts})
        return out
    except Exception as e:
        print(f"[yf news] {yf_code}: {e}")
        return []


GEMINI_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")


def _gemini_call(key: str, prompt: str) -> str:
    """新版 google-genai SDK；依序嘗試多個模型，回傳首個成功結果文字。"""
    from google import genai
    client = genai.Client(api_key=key)
    last_err = None
    for model_name in GEMINI_MODELS:
        try:
            resp = client.models.generate_content(model=model_name, contents=prompt)
            txt = (resp.text or "").strip()
            if txt:
                return txt
        except Exception as e:
            last_err = e
            continue
    raise last_err or RuntimeError("Gemini all models failed")


def _gemini_translate_batch(titles: list[str], key: str) -> list[str] | None:
    """Gemini 批次翻譯（一次 API call 翻全部）。失敗回 None。"""
    try:
        prompt = (
            "把以下英文新聞標題翻譯成繁體中文（台灣用語），保持簡潔自然，"
            "每行一條翻譯對應一條原文，順序對齊，不要加編號或解釋：\n\n"
            + "\n".join(titles)
        )
        text = _gemini_call(key, prompt)
        lines = [_re.sub(r"^\s*\d+[\.\)]\s*", "", l).strip()
                 for l in text.split("\n") if l.strip()]
        # 行數要對齊；不齊就退到 None 走 fallback
        if len(lines) == len(titles):
            return lines
        if len(lines) >= len(titles):
            return lines[:len(titles)]
        return None
    except Exception as e:
        print(f"[translate-gemini] {e}")
        return None


def _gtx_translate_one(text: str) -> str | None:
    """Google Translate 免費（unofficial）端點，單筆翻譯，無需 API key。"""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "zh-TW", "dt": "t", "q": text},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()
        # data[0] 為分段陣列，每段 [譯文, 原文, ...]
        chunks = data[0] or []
        out = "".join((c[0] or "") for c in chunks if c)
        return out.strip() or None
    except Exception as e:
        print(f"[translate-gtx] {e}")
        return None


def _translate_titles_zh(titles: list[str]) -> list[str]:
    """翻譯英文新聞標題為繁體中文。
    優先用 Gemini（品質好+一次批次）；沒設 key 或失敗時退回 Google Translate
    免費端點（無 key、逐條翻譯）；最終 fallback 才回傳原英文。
    """
    if not titles:
        return titles

    cache_k = f"trans:{hash(tuple(titles))}"
    cached = cache_get(cache_k)
    if cached:
        return cached

    # 1) Gemini 批次（如果有 key）
    key = _get_gemini_key()
    if key:
        result = _gemini_translate_batch(titles, key)
        if result:
            cache_set(cache_k, result)
            return result

    # 2) Google Translate 免費端點（逐條）
    out = []
    success = 0
    for t in titles:
        zh = _gtx_translate_one(t)
        if zh:
            out.append(zh); success += 1
        else:
            out.append(t)  # 翻譯失敗保留原文
    if success > 0:
        cache_set(cache_k, out)
        return out

    # 3) 全部失敗 → 原文
    return titles


def fetch_news(code: str) -> list:
    cached = cache_get(f"news:{code}")
    if cached:
        return cached
    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        return []

    raw = _fetch_yfinance_news(info["yf"])
    if not raw:
        return []

    # 批次翻譯標題（一次 API call）
    orig_titles = [n["title"] for n in raw]
    zh_titles   = _translate_titles_zh(orig_titles)

    out = []
    for i, n in enumerate(raw):
        out.append({
            "title":      zh_titles[i] if i < len(zh_titles) else n["title"],
            "title_orig": n["title"],
            "publisher":  n["publisher"],
            "link":       n["link"],
            "time":       n["time"],
        })
    cache_set(f"news:{code}", out)
    return out


# ----------------------------------------------------------------------------
# 內部人交易 (yfinance.Ticker.insider_transactions，免 key 即時)
# ----------------------------------------------------------------------------
def fetch_insider(code: str, days: int = 180) -> dict:
    """近 N 天的 SEC Form 4 內部人交易，分類 buy/sell 並算淨值。
    cache 1 小時（內部人通常幾天才動一次）。
    """
    cache_key = f"insider:{code}:{days}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        return {"transactions": [], "summary": {"buy_value": 0, "sell_value": 0, "net_value": 0,
                                                  "buy_count": 0, "sell_count": 0}}
    try:
        df = yf.Ticker(info["yf"]).insider_transactions
    except Exception as e:
        print(f"[insider] {code}: {e}")
        df = None
    if df is None or df.empty:
        out = {"transactions": [], "summary": {"buy_value": 0, "sell_value": 0, "net_value": 0,
                                                 "buy_count": 0, "sell_count": 0}}
        cache_set(cache_key, out)
        return out

    df = df.copy()
    df["Start Date"] = pd.to_datetime(df["Start Date"], errors="coerce")
    df = df.dropna(subset=["Start Date"])
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=days)
    df = df[df["Start Date"] >= cutoff].sort_values("Start Date", ascending=False)

    buy_total = 0.0
    sell_total = 0.0
    txs = []
    for _, r in df.iterrows():
        txt = str(r.get("Text", ""))
        if "Purchase" in txt or "Buy" in txt:
            action = "buy"
        elif "Sale" in txt or "Sell" in txt or "Sold" in txt:
            action = "sell"
        else:
            action = "other"
        # yfinance 某些紀錄（如贈與、選擇權行權）的 Value/Shares 可能是 NaN
        raw_val = r.get("Value")
        val = float(raw_val) if raw_val is not None and not pd.isna(raw_val) else 0.0
        raw_sh = r.get("Shares")
        shares = int(raw_sh) if raw_sh is not None and not pd.isna(raw_sh) else 0
        if action == "buy":  buy_total += val
        elif action == "sell": sell_total += val
        txs.append({
            "date":     r["Start Date"].strftime("%Y-%m-%d"),
            "insider":  str(r.get("Insider", "")),
            "position": str(r.get("Position", "")),
            "action":   action,
            "shares":   shares,
            "value":    int(val),
            "text":     txt[:60],
        })

    net = buy_total - sell_total
    out = {
        "code": code,
        "days": days,
        "transactions": txs[:30],
        "summary": {
            "buy_value":  int(buy_total),
            "sell_value": int(sell_total),
            "net_value":  int(net),
            "buy_count":  sum(1 for t in txs if t["action"] == "buy"),
            "sell_count": sum(1 for t in txs if t["action"] == "sell"),
            "total_count": len(txs),
        }
    }
    # cache 1 小時
    _cache[cache_key] = (time.time() + 3600 - CACHE_TTL, out)
    return out


# ----------------------------------------------------------------------------
# 13F 機構持股 (yfinance Ticker.institutional_holders / major_holders)
# ----------------------------------------------------------------------------
def fetch_institutional_holders(code: str) -> dict:
    """Top 10 機構持股 + Top 5 mutual fund + 整體機構/內部人 %。
    SEC 13F 是季報延遲 45 天，cache 6 小時即可。
    """
    cache_key = f"inst13f:{code}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404)
    try:
        t = yf.Ticker(info["yf"])
        inst_df = t.institutional_holders
        mf_df   = t.mutualfund_holders
        major   = t.major_holders
    except Exception as e:
        print(f"[13F] {code}: {e}")
        inst_df = mf_df = major = None

    def parse_holder(df, top_n):
        out = []
        if df is None or df.empty:
            return out
        for _, r in df.head(top_n).iterrows():
            shares = r.get("Shares")
            value  = r.get("Value")
            pct_h  = r.get("pctHeld")
            pct_c  = r.get("pctChange")
            date_r = r.get("Date Reported")
            out.append({
                "holder":     str(r.get("Holder", "")),
                "shares":     int(shares) if pd.notna(shares) else 0,
                "value":      int(value)  if pd.notna(value)  else 0,
                "pct_held":   round(float(pct_h) * 100, 2) if pd.notna(pct_h) else 0,
                "pct_change": round(float(pct_c) * 100, 2) if pd.notna(pct_c) else 0,
                "date":       str(date_r) if pd.notna(date_r) else "",
            })
        return out

    inst = parse_holder(inst_df, 10)
    mf   = parse_holder(mf_df, 5)

    pct_insider = pct_institutions = 0.0
    n_institutions = 0
    if major is not None and not major.empty:
        try:
            if "insidersPercentHeld" in major.index:
                pct_insider = float(major.loc["insidersPercentHeld"].iloc[0]) * 100
            if "institutionsPercentHeld" in major.index:
                pct_institutions = float(major.loc["institutionsPercentHeld"].iloc[0]) * 100
            if "institutionsCount" in major.index:
                n_institutions = int(major.loc["institutionsCount"].iloc[0])
        except Exception:
            pass

    top10_pct = sum(h["pct_held"] for h in inst)

    out = {
        "code": code,
        "institutional": inst,
        "mutualfund":    mf,
        "summary": {
            "pct_insider":      round(pct_insider, 2),
            "pct_institutions": round(pct_institutions, 2),
            "top10_pct":        round(top10_pct, 2),
            "n_institutions":   n_institutions,
            "n_top_holders":    len(inst),
        }
    }
    # cache 6 小時 (13F 季報，每天頂多動一兩家)
    _cache[cache_key] = (time.time() + 6 * 3600 - CACHE_TTL, out)
    return out


# ----------------------------------------------------------------------------
# 估值指標 (yfinance Ticker.info) + 同族群相對位置
# ----------------------------------------------------------------------------
def fetch_valuation(code: str) -> dict:
    """單股估值 dict (cache 6 hr)。"""
    cache_key = f"valn:{code}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info_meta = wl.get(code)
    if not info_meta:
        raise HTTPException(404)
    try:
        info = yf.Ticker(info_meta["yf"]).info or {}
    except Exception as e:
        print(f"[valn] {code}: {e}")
        info = {}

    def num(k):
        v = info.get(k)
        if v is None: return None
        try:
            v = float(v)
            return v if not (v != v) else None  # filter NaN
        except (TypeError, ValueError):
            return None

    out = {
        "code":  code,
        "name":  info_meta.get("name", code),
        "group": info_meta.get("group", "自選"),
        "trailing_pe":  num("trailingPE"),
        "forward_pe":   num("forwardPE"),
        "price_book":   num("priceToBook"),
        "price_sales":  num("priceToSalesTrailing12Months"),
        "peg":          num("trailingPegRatio") or num("pegRatio"),
        "ev_ebitda":    num("enterpriseToEbitda"),
        "div_yield":    num("dividendYield"),
        "profit_margin": num("profitMargins"),
        "roe":          num("returnOnEquity"),
        "earnings_growth": num("earningsGrowth"),
        "revenue_growth":  num("revenueGrowth"),
        "beta":         num("beta"),
        "market_cap":   num("marketCap"),
    }
    _cache[cache_key] = (time.time() + 6 * 3600 - CACHE_TTL, out)
    return out


def _percentile_rank(values: list[float], target: float) -> float | None:
    """計算 target 在 values 中的百分位 (0-100)，target 是低值 → 百分位低。"""
    vals = [v for v in values if v is not None and v > 0]
    if not vals or target is None:
        return None
    below = sum(1 for v in vals if v < target)
    return round(below / len(vals) * 100, 1)


def fetch_valuation_ranking() -> list:
    """所有 watchlist 股的估值 + 同族群相對位置。"""
    cache_key = "valn-ranking"
    cached = cache_get(cache_key)
    if cached:
        return cached

    from concurrent.futures import ThreadPoolExecutor
    codes = list(load_watchlist().keys())

    def safe(c):
        try: return fetch_valuation(c)
        except: return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        items = [r for r in ex.map(safe, codes) if r]

    # 各族群 PE / PEG / P/B 中位數
    from statistics import median
    by_group: dict[str, list[dict]] = {}
    for it in items:
        by_group.setdefault(it["group"], []).append(it)

    group_stats: dict[str, dict] = {}
    for g, members in by_group.items():
        pes  = [m["trailing_pe"] for m in members if m["trailing_pe"] and m["trailing_pe"] > 0]
        pegs = [m["peg"]          for m in members if m["peg"] and m["peg"] > 0]
        pbs  = [m["price_book"]   for m in members if m["price_book"] and m["price_book"] > 0]
        group_stats[g] = {
            "pe_median":   round(median(pes), 2)  if pes  else None,
            "peg_median":  round(median(pegs), 2) if pegs else None,
            "pb_median":   round(median(pbs), 2)  if pbs  else None,
            "n_members":   len(members),
        }

    # 計算每股相對位置（百分位 + 偏離 %）
    for it in items:
        g = it["group"]
        g_pes  = [m["trailing_pe"] for m in by_group[g] if m["trailing_pe"]]
        g_pegs = [m["peg"]          for m in by_group[g] if m["peg"]]
        gs = group_stats[g]
        it["pe_percentile"]  = _percentile_rank(g_pes,  it["trailing_pe"])
        it["peg_percentile"] = _percentile_rank(g_pegs, it["peg"])
        it["pe_vs_group"]    = (round((it["trailing_pe"] - gs["pe_median"]) / gs["pe_median"] * 100, 1)
                                if gs.get("pe_median") and it.get("trailing_pe") else None)
        it["peg_vs_group"]   = (round((it["peg"] - gs["peg_median"]) / gs["peg_median"] * 100, 1)
                                if gs.get("peg_median") and it.get("peg") else None)
        it["group_stats"]    = gs

    cache_set(cache_key, items)
    return items


def insider_signals(code: str) -> list[dict]:
    """從近 90 日內部人交易產生訊號徽章。"""
    try:
        ins = fetch_insider(code, days=90)
    except Exception:
        return []
    s = ins.get("summary", {})
    net = s.get("net_value", 0)
    sigs = []
    if net >= 500_000:
        sigs.append({"key": "insider_buy", "label": "🐳 內部人大買", "color": "red"})
    elif net <= -5_000_000:
        sigs.append({"key": "insider_sell_heavy", "label": "📉 內部人大賣", "color": "green"})
    elif net <= -1_000_000:
        sigs.append({"key": "insider_sell", "label": "⚠️ 內部人賣超", "color": "orange"})
    return sigs


# ----------------------------------------------------------------------------
# 探測股票代號 (.TW 或 .TWO)
# ----------------------------------------------------------------------------
def probe_yfinance(code: str) -> dict | None:
    """美股代號直接用 ticker 本身，不必加 .TW / .TWO 後綴。"""
    code = code.strip().upper()
    try:
        t = yf.Ticker(code)
        h = t.history(period="5d", interval="1d", auto_adjust=False)
        if h.empty:
            return None
        try:
            inf = t.info or {}
        except Exception:
            inf = {}
        name = inf.get("longName") or inf.get("shortName") or code
        sector = inf.get("sector") or inf.get("industry") or "—"
        return {
            "code":   code,
            "yf":     code,
            "name":   name,
            "sector": sector,
            "price":  float(h.iloc[-1]["Close"]),
        }
    except Exception as e:
        print(f"[probe] {code}: {e}")
        return None


# ----------------------------------------------------------------------------
# Telegram 警示
# ----------------------------------------------------------------------------
def send_telegram(text: str) -> bool:
    cfg = load_telegram()
    token = cfg.get("bot_token", "")
    chat = cfg.get("chat_id", "")
    if not token or not chat:
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
        print(f"[telegram] {e}")
        return False


def check_alert(code: str, price: float, prev: float, name: str = "") -> None:
    alerts = load_alerts()
    rule = alerts.get(code)
    if not rule:
        return
    above = rule.get("above")
    below = rule.get("below")
    last_price = rule.get("last_price", prev)
    triggered = []
    if above is not None and float(last_price) < float(above) <= price:
        triggered.append(f"🚀 *{name} ({code})* 突破上方警示\n價位: *{above}* → 現價 *{price:.2f}*")
    if below is not None and float(last_price) > float(below) >= price:
        triggered.append(f"⚠️ *{name} ({code})* 跌破下方警示\n價位: *{below}* → 現價 *{price:.2f}*")
    rule["last_price"] = price
    alerts[code] = rule
    save_json(ALERTS_FILE, alerts)
    for msg in triggered:
        send_telegram(msg)


def alert_worker():
    """背景每 5 分鐘掃描有設警示的股票。"""
    print("[alert_worker] 啟動，每 5 分鐘檢查一次")
    while True:
        time.sleep(300)
        try:
            wl = load_watchlist()
            alerts = load_alerts()
            for code in alerts:
                if code not in wl:
                    continue
                try:
                    fetch_stock(code, force=True)  # 內部會 check_alert
                except Exception as e:
                    print(f"[alert_worker] {code}: {e}")
        except Exception as e:
            print(f"[alert_worker] {e}")


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------
app = FastAPI(title="美股情報站 API", version="1.0")

# CORS：允許從 GitHub Pages、file://、其他主機載入的前端訪問本機 server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def fetch_summary(code: str) -> dict:
    """輕量版：只抓最近 30 個交易日，不算複雜指標、不打 FinMind。

    主要給清單頁用，冷快取下也能在 < 5 秒內回應 16 檔。
    若 fetch_stock 已有完整快取則直接重用。
    """
    full = cache_get(f"stock:{code}:D")
    if full:
        return {
            "code":   code,
            "name":   full["name"],
            "tag":    full["tag"],
            "group":  full.get("group", "自選"),
            "price":  full["price"],
            "prev":   full["prev"],
            "asOf":   full["asOf"],
            "signals": full.get("signals", []),
        }

    cached = cache_get(f"summary:{code}")
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404, f"未追蹤股票 {code}")

    try:
        hist = yf.Ticker(info["yf"]).history(period="2mo", interval="1d", auto_adjust=False)
    except Exception as e:
        raise HTTPException(503, f"yfinance 失敗: {e}")
    if hist.empty:
        raise HTTPException(503, f"yfinance 取不到 {code}")

    closes = hist["Close"].dropna()
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else last

    # 訊號：用本地的 30 根日 K 簡算
    ma5_s  = sma(closes, 5)
    ma20_s = sma(closes, 20)
    rsi_s  = rsi_indicator(closes, 14)
    k_s, d_s = kd_indicator(hist, 9)
    sigs = detect_signals(closes, ma5_s, ma20_s, k_s, d_s, rsi_s, hist["High"], hist["Low"], period="D")
    # 加上內部人訊號（cache 1 hr，不會拖慢 summary 列表）
    sigs.extend(insider_signals(code))

    # 短線勝率啟發式 (與前端 winRate 計算一致)
    rsi_v = float(rsi_s.iloc[-1]) if not pd.isna(rsi_s.iloc[-1]) else 50.0
    ma5v  = float(ma5_s.iloc[-1])  if not pd.isna(ma5_s.iloc[-1])  else last
    ma20v = float(ma20_s.iloc[-1]) if not pd.isna(ma20_s.iloc[-1]) else last
    base = 50
    if ma5v > ma20v: base += 15
    elif ma5v < ma20v: base -= 15
    if rsi_v > 70: base -= 10
    elif rsi_v < 30: base += 10
    win_rate = max(20, min(85, base))

    out = {
        "code":   code,
        "name":   info["name"],
        "tag":    info["tag"],
        "group":  info.get("group", "自選"),
        "price":  round(last, 2),
        "prev":   round(prev, 2),
        "asOf":   str(hist.index[-1].date()),
        "signals": sigs,
        "rsi":          round(rsi_v, 2),
        "win_rate":     win_rate,
        "signal_count": len(sigs),
    }
    cache_set(f"summary:{code}", out)
    return out


@app.get("/api/stocks")
def api_list():
    """觀察清單摘要 (輕量版，並行抓取)。"""
    from concurrent.futures import ThreadPoolExecutor

    wl = load_watchlist()
    codes = list(wl.keys())

    def safe_summary(code):
        try:
            return fetch_summary(code)
        except Exception as e:
            meta = wl.get(code, {})
            return {
                "code": code, "name": meta.get("name", code), "tag": meta.get("tag", ""),
                "group": meta.get("group", "自選"), "error": str(e),
            }

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(safe_summary, codes))
    return results


@app.get("/api/stock/{code}")
def api_stock(code: str, period: str = "D"):
    try:
        return fetch_stock(code, period=period)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/news/{code}")
def api_news(code: str):
    return fetch_news(code)


@app.get("/api/insider/{code}")
def api_insider(code: str, days: int = 180):
    return fetch_insider(code, days=days)


@app.get("/api/institutional/{code}")
def api_institutional(code: str):
    """Top 10 機構持股 + Top 5 mutual fund + 整體 % (13F 來自 yfinance)。"""
    return fetch_institutional_holders(code)


@app.get("/api/valuation/{code}")
def api_valuation(code: str):
    """單股估值指標 (P/E, PEG, P/B 等) 含同族群相對位置。"""
    v = fetch_valuation(code)
    # 補上族群相對位置 (從整個 ranking 拿)
    try:
        all_items = fetch_valuation_ranking()
        match = next((x for x in all_items if x["code"] == code), None)
        if match:
            v["pe_percentile"]  = match.get("pe_percentile")
            v["peg_percentile"] = match.get("peg_percentile")
            v["pe_vs_group"]    = match.get("pe_vs_group")
            v["peg_vs_group"]   = match.get("peg_vs_group")
            v["group_stats"]    = match.get("group_stats", {})
    except Exception:
        pass
    return v


@app.get("/api/valuation")
def api_valuation_all():
    """全 watchlist 估值排行 + 各族群中位數。"""
    return fetch_valuation_ranking()


@app.get("/api/groups")
def api_groups():
    """族群清單 + 每族群成員代號。"""
    wl = load_watchlist()
    groups: dict[str, list[str]] = {}
    for code, s in wl.items():
        g = s.get("group", "自選")
        groups.setdefault(g, []).append(code)
    return groups


@app.get("/api/ranking")
def api_ranking(by: str = "change"):
    """熱度榜排序：
    - change / down: 漲幅 / 跌幅
    - volume:        成交量
    - fi / fi_sell:  Strong Buy 多 / 少
    - rsi / rsi_low: RSI 高 / 低
    - win:           短線勝率
    - signals:       訊號數
    - bias:          乖離率
    """
    items = []
    for code in load_watchlist():
        try:
            d = fetch_stock(code)
            change_pct = (d["price"] - d["prev"]) / d["prev"] * 100 if d["prev"] else 0
            sigs = d.get("signals", [])
            ma_status = d.get("maStatus", "")
            rsi_v = d["rsi"]
            base = 50
            if "多頭" in ma_status: base += 15
            elif "空頭" in ma_status: base -= 15
            if rsi_v > 70: base -= 10
            elif rsi_v < 30: base += 10
            win_rate = max(20, min(85, base))
            ma20 = d.get("ma20", 0) or 1
            bias = (d["price"] - ma20) / ma20 * 100 if ma20 else 0
            # 13F 共識度（cache 6 hr）
            inst_pct = inst_top10 = 0
            try:
                ih = fetch_institutional_holders(code)
                inst_pct   = ih["summary"].get("pct_institutions", 0)
                inst_top10 = ih["summary"].get("top10_pct", 0)
            except Exception:
                pass
            # 估值指標 (cache 6 hr)
            peg = pe = pe_vs_group = None
            try:
                v = fetch_valuation(code)
                peg = v.get("peg")
                pe  = v.get("trailing_pe")
            except Exception:
                pass
            items.append({
                "code":       code,
                "name":       d["name"],
                "tag":        d["tag"],
                "group":      d.get("group", "自選"),
                "price":      d["price"],
                "prev":       d["prev"],
                "change_pct": round(change_pct, 2),
                "volume":     d["volume"],
                "avgVol":     d["avgVol"],
                "vol_change": d["volChange"],
                "fi_today":   d["chip"]["fi_today"],
                "it_today":   d["chip"]["it_today"],
                "rsi":        rsi_v,
                "trend":      d["trend"],
                "signals":    sigs,
                "signal_count": len(sigs),
                "win_rate":   win_rate,
                "bias":       round(bias, 2),
                "risk":       d["risk"],
                "inst_pct":    inst_pct,
                "inst_top10":  inst_top10,
                "pe":          pe,
                "peg":         peg,
            })
        except Exception:
            pass
    # 計算族群相對 PE (在 ranking 端點即時算，因 valuation_ranking 也有 cache)
    try:
        valn_items = fetch_valuation_ranking()
        rel_map = {v["code"]: v.get("pe_vs_group") for v in valn_items}
        for it in items:
            it["pe_vs_group"] = rel_map.get(it["code"])
    except Exception:
        for it in items:
            it.setdefault("pe_vs_group", None)

    def _peg_key(x):
        v = x.get("peg")
        return v if (v is not None and v > 0) else 9999
    def _relpe_key(x):
        v = x.get("pe_vs_group")
        return v if v is not None else 9999

    keymap = {
        "change":   lambda x: -x["change_pct"],
        "down":     lambda x:  x["change_pct"],
        "volume":   lambda x: -x["volume"],
        "fi":       lambda x: -x["fi_today"],
        "fi_sell":  lambda x:  x["fi_today"],
        "rsi":      lambda x: -x["rsi"],
        "rsi_low":  lambda x:  x["rsi"],
        "win":      lambda x: -x["win_rate"],
        "signals":  lambda x: -x["signal_count"],
        "bias":     lambda x: -abs(x["bias"]),
        "inst":     lambda x: -x["inst_pct"],
        "inst10":   lambda x: -x["inst_top10"],
        "peg":      _peg_key,            # PEG 最低（< 1 = 相對便宜）
        "relpe":    _relpe_key,          # 相對族群 P/E 最低（同族群最便宜）
    }
    items.sort(key=keymap.get(by, keymap["change"]))
    return items


# ----- 觀察清單管理 -----
class AddStockReq(BaseModel):
    code:  str
    name:  Optional[str] = None
    tag:   Optional[str] = None
    group: Optional[str] = "自選"


@app.get("/api/watchlist")
def api_get_watchlist():
    return load_watchlist()


@app.post("/api/watchlist")
def api_add_watchlist(req: AddStockReq):
    # 美股代號為字母 (AAPL / NVDA)、ETF 含數字或加 . (BRK.B)、指數含 ^ (^GSPC)
    code = req.code.strip().upper()
    if not code or len(code) > 10 or not all(c.isalnum() or c in ".-" for c in code):
        raise HTTPException(400, "代號格式不正確（僅允許英數字、. 或 -，最多 10 字元）")
    wl = load_watchlist()
    if code in wl:
        return {"ok": True, "msg": "已在觀察清單", "data": wl[code]}
    probe = probe_yfinance(code)
    if not probe:
        raise HTTPException(404, f"yfinance 找不到 {code}（試過 .TW / .TWO）")
    name = req.name or probe["name"]
    if len(name) > 12:
        name = name[:12]
    wl[code] = {
        "name":  name,
        "tag":   req.tag or probe.get("sector", "—"),
        "yf":    probe["yf"],
        "group": req.group or "自選",
    }
    save_json(WATCHLIST_FILE, wl)
    # 清掉清單快取，下次列表會重撈
    _cache.pop(f"stock:{code}:D", None)
    return {"ok": True, "data": wl[code], "probe": probe}


@app.delete("/api/watchlist/{code}")
def api_del_watchlist(code: str):
    wl = load_watchlist()
    if code not in wl:
        raise HTTPException(404)
    del wl[code]
    save_json(WATCHLIST_FILE, wl)
    return {"ok": True}


@app.get("/api/probe/{code}")
def api_probe(code: str):
    p = probe_yfinance(code)
    if not p:
        raise HTTPException(404, "yfinance 找不到")
    return p


# ----- Telegram -----
class TelegramReq(BaseModel):
    bot_token: str
    chat_id:   str


@app.post("/api/telegram")
def api_telegram_set(req: TelegramReq):
    save_json(TELEGRAM_FILE, {
        "bot_token": req.bot_token.strip(),
        "chat_id":   req.chat_id.strip(),
    })
    ok = send_telegram(f"✅ *美股情報站* Telegram 連線測試成功\n時間: `{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}`")
    return {"ok": True, "test_sent": ok}


@app.get("/api/telegram")
def api_telegram_get():
    cfg = load_telegram()
    return {
        "configured": bool(cfg.get("bot_token") and cfg.get("chat_id")),
        "chat_id":    cfg.get("chat_id", ""),
    }


# ----- Alerts -----
class AlertReq(BaseModel):
    above: Optional[float] = None
    below: Optional[float] = None


@app.get("/api/alerts")
def api_get_alerts():
    return load_alerts()


@app.post("/api/alerts/{code}")
def api_set_alert(code: str, req: AlertReq):
    alerts = load_alerts()
    rule = alerts.get(code, {})
    rule["above"] = req.above
    rule["below"] = req.below
    if "last_price" not in rule:
        try:
            d = fetch_stock(code)
            rule["last_price"] = d["price"]
        except Exception:
            rule["last_price"] = 0
    alerts[code] = rule
    save_json(ALERTS_FILE, alerts)
    return {"ok": True, "data": rule}


@app.delete("/api/alerts/{code}")
def api_del_alert(code: str):
    alerts = load_alerts()
    alerts.pop(code, None)
    save_json(ALERTS_FILE, alerts)
    return {"ok": True}


# ----- Misc -----
@app.get("/api/refresh")
def api_refresh():
    _cache.clear()
    return {"ok": True, "msg": "快取已清除"}


@app.post("/api/seed-defaults")
def api_seed_defaults():
    """把 DEFAULT_WATCHLIST 中還沒在 watchlist.json 的股票補進去。
    用於升級族群分類後一鍵加入新建議標的（不會碰使用者既有清單）。
    """
    wl = load_watchlist()
    added = []
    for code, meta in DEFAULT_WATCHLIST.items():
        if code not in wl:
            wl[code] = dict(meta)
            added.append(code)
    if added:
        save_json(WATCHLIST_FILE, wl)
        # 清快取讓清單與排行重新計算
        for k in list(_cache.keys()):
            if k.startswith("summary:") or k.startswith("stock:") or k == "valn-ranking":
                _cache.pop(k, None)
    return {"ok": True, "added": added, "n_added": len(added),
            "msg": f"已新增 {len(added)} 檔到觀察清單"}


# ============================================================================
# 1) Portfolio (個人持股；同代號可多筆，各自有 id)
# ============================================================================
import uuid as _uuid


def load_portfolio() -> list:
    """永遠回 list。舊版 dict 格式 (code → holding) 會自動遷移成新版 list。"""
    raw = load_json(PORTFOLIO_FILE, [])
    if isinstance(raw, dict):
        migrated = []
        for code, h in raw.items():
            migrated.append({
                "id":         _uuid.uuid4().hex[:12],
                "code":       code,
                "shares":     float(h.get("shares", 0) or 0),
                "cost_price": float(h.get("cost_price", 0) or 0),
                "buy_date":   h.get("buy_date", "") or "",
                "note":       h.get("note", "") or "",
            })
        save_json(PORTFOLIO_FILE, migrated)
        print(f"[portfolio] 舊 dict 格式遷移成 list，{len(migrated)} 筆")
        return migrated
    return raw if isinstance(raw, list) else []


class HoldingReq(BaseModel):
    code:       str
    shares:     float       # 美股股數（1 股 = 1 股）
    cost_price: float
    buy_date:   Optional[str] = None
    note:       Optional[str] = ""


def _trailing_stop_advice(yf_code: str, buy_date: str, cost_price: float, current_price: float) -> dict:
    """計算移動停利建議。

    規則組合：
    - 從買入日後的最高點 -10% = 移動停利
    - 保本停利：若已 +5% 起，停損上移至成本價
    - 目標停利：+20%
    """
    try:
        kwargs = {"period": "1y", "interval": "1d", "auto_adjust": False}
        if buy_date:
            kwargs = {"start": buy_date, "interval": "1d", "auto_adjust": False}
        hist = yf.Ticker(yf_code).history(**kwargs)
        if hist.empty:
            return {"trail_stop": None, "post_high": None, "breakeven": None,
                    "target": round(cost_price * 1.20, 2), "rule": "資料不足"}
        post_high = float(hist["High"].max())
    except Exception as e:
        print(f"[trail] {yf_code}: {e}")
        return {"trail_stop": None, "post_high": None, "breakeven": None,
                "target": round(cost_price * 1.20, 2), "rule": "計算失敗"}

    trail_pct  = 0.10
    trail_stop = round(post_high * (1 - trail_pct), 2)
    target     = round(cost_price * 1.20, 2)
    breakeven  = round(cost_price, 2) if current_price >= cost_price * 1.05 else None
    ret_pct    = (current_price - cost_price) / cost_price * 100 if cost_price else 0

    if ret_pct >= 20:
        rule = f"已 +{ret_pct:.0f}% 達目標，建議分批停利"
    elif current_price <= trail_stop:
        rule = f"已觸發 -10% 移動停利 ({trail_stop})，建議出場"
    elif breakeven:
        rule = f"已保本；停利上移至高點 -10% = {trail_stop}"
    else:
        rule = f"持有觀察，停損 {round(cost_price * 0.92, 2)} (-8%)"

    return {
        "trail_stop": trail_stop,
        "post_high":  round(post_high, 2),
        "breakeven":  breakeven,
        "target":     target,
        "rule":       rule,
    }


@app.get("/api/portfolio")
def api_get_portfolio():
    """回傳所有持股 + 市值/損益/移動停利建議（用即時收盤價計算）。"""
    p = load_portfolio()
    if not p:
        return {"holdings": [], "summary": {"total_cost": 0, "total_value": 0,
                                              "total_pnl": 0, "total_pnl_pct": 0, "count": 0}}
    holdings = []
    total_cost = 0.0
    total_value = 0.0
    wl = load_watchlist()
    for h in p:
        code = h["code"]
        try:
            s = fetch_summary(code)
            price = float(s["price"])
            name  = s["name"]
            tag   = s.get("tag", "")
        except Exception:
            price, name, tag = float(h["cost_price"]), code, ""
        yf_code = wl.get(code, {}).get("yf", code)
        shares = float(h["shares"])
        cost   = shares * float(h["cost_price"])
        value  = shares * price
        pnl    = value - cost
        pnl_pct = (price - float(h["cost_price"])) / float(h["cost_price"]) * 100 if h["cost_price"] else 0
        total_cost += cost
        total_value += value

        trail = _trailing_stop_advice(yf_code, h.get("buy_date", ""), float(h["cost_price"]), price)

        holdings.append({
            "id":         h.get("id") or _uuid.uuid4().hex[:12],
            "code":       code,
            "name":       name,
            "tag":        tag,
            "shares":     shares,
            "cost_price": float(h["cost_price"]),
            "buy_date":   h.get("buy_date", ""),
            "note":       h.get("note", ""),
            "current":    round(price, 2),
            "cost":       round(cost, 0),
            "value":      round(value, 0),
            "pnl":        round(pnl, 0),
            "pnl_pct":    round(pnl_pct, 2),
            "trail_stop": trail.get("trail_stop"),
            "post_high":  trail.get("post_high"),
            "breakeven":  trail.get("breakeven"),
            "target":     trail.get("target"),
            "rule":       trail.get("rule"),
        })
    for h in holdings:
        h["weight"] = round(h["value"] / total_value * 100, 1) if total_value > 0 else 0
    # 預設按名稱字母排（前端可再以欄位點擊重新排序）
    holdings.sort(key=lambda x: (x.get("name") or x.get("code") or "").lower())

    summary = {
        "total_cost":    round(total_cost, 0),
        "total_value":   round(total_value, 0),
        "total_pnl":     round(total_value - total_cost, 0),
        "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0,
        "count":         len(holdings),
    }
    return {"holdings": holdings, "summary": summary}


@app.post("/api/portfolio")
def api_add_portfolio(req: HoldingReq):
    code = req.code.strip().upper()
    if not code or not all(c.isalnum() or c in ".-" for c in code):
        raise HTTPException(400, "代號格式不正確")
    if req.shares <= 0 or req.cost_price <= 0:
        raise HTTPException(400, "股數與成本價需 > 0")

    p = load_portfolio()  # list

    # 若不在 watchlist，順手加入（便於追蹤），並清掉相關快取讓 chip 即時更新
    wl = load_watchlist()
    added_to_watchlist = False
    if code not in wl:
        probe = probe_yfinance(code)
        if probe:
            wl[code] = {
                "name":  probe["name"][:12],
                "tag":   probe.get("sector", "—"),
                "yf":    probe["yf"],
                "group": "持股",
            }
            save_json(WATCHLIST_FILE, wl)
            added_to_watchlist = True
            # Clear caches so /api/stocks chip count refresh
            for k in list(_cache.keys()):
                if k.startswith("summary:") or k.startswith("stock:"):
                    _cache.pop(k, None)

    new_holding = {
        "id":         _uuid.uuid4().hex[:12],
        "code":       code,
        "shares":     float(req.shares),
        "cost_price": float(req.cost_price),
        "buy_date":   req.buy_date or "",
        "note":       req.note or "",
    }
    p.append(new_holding)
    save_json(PORTFOLIO_FILE, p)
    return {"ok": True, "data": new_holding, "added_to_watchlist": added_to_watchlist}


@app.delete("/api/portfolio/{holding_id}")
def api_del_portfolio(holding_id: str):
    """刪除單筆持股 (用 id)。向下相容：若傳入的是 code 且唯一，也接受。"""
    p = load_portfolio()
    new_p = [h for h in p if h.get("id") != holding_id]
    if len(new_p) == len(p):
        new_p = [h for h in p if h.get("code") != holding_id]
        if len(new_p) == len(p):
            raise HTTPException(404)
    save_json(PORTFOLIO_FILE, new_p)
    return {"ok": True, "removed": len(p) - len(new_p)}


# ============================================================================
# 2) Signal backtest (訊號績效回測)
# ============================================================================
@app.get("/api/signal-stats/{code}/{signal_key}")
def api_signal_stats(code: str, signal_key: str, forward: int = 5):
    """掃過去 2 年該訊號出現的歷史，計算後 N 天平均報酬與勝率。"""
    cache_key = f"sigstat:{code}:{signal_key}:{forward}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404)

    try:
        hist = yf.Ticker(info["yf"]).history(period="2y", interval="1d", auto_adjust=False)
    except Exception as e:
        raise HTTPException(503, str(e))
    if len(hist) < 60:
        raise HTTPException(503, "歷史資料不足")
    hist = hist.dropna(subset=["Close"])

    closes = hist["Close"]
    ma5_s, ma20_s = sma(closes, 5), sma(closes, 20)
    rsi_s = rsi_indicator(closes, 14)
    k_s, d_s = kd_indicator(hist, 9)

    occurrences = []
    for i in range(60, len(hist) - forward):
        snap_close = closes.iloc[: i + 1]
        snap_ma5   = ma5_s.iloc[: i + 1]
        snap_ma20  = ma20_s.iloc[: i + 1]
        snap_rsi   = rsi_s.iloc[: i + 1]
        snap_k     = k_s.iloc[: i + 1]
        snap_d     = d_s.iloc[: i + 1]
        snap_h     = hist["High"].iloc[: i + 1]
        snap_l     = hist["Low"].iloc[: i + 1]
        sigs = detect_signals(snap_close, snap_ma5, snap_ma20, snap_k, snap_d,
                              snap_rsi, snap_h, snap_l, period="D")
        if any(s["key"] == signal_key for s in sigs):
            entry = float(closes.iloc[i])
            future = float(closes.iloc[i + forward])
            ret = (future - entry) / entry * 100
            occurrences.append({
                "date":   hist.index[i].strftime("%Y-%m-%d"),
                "entry":  round(entry, 2),
                "future": round(future, 2),
                "return": round(ret, 2),
            })

    if not occurrences:
        result = {"signal": signal_key, "count": 0, "msg": "歷史中無此訊號"}
    else:
        rets = [o["return"] for o in occurrences]
        win = sum(1 for r in rets if r > 0)
        result = {
            "signal":      signal_key,
            "code":        code,
            "name":        info["name"],
            "forward_days": forward,
            "count":       len(occurrences),
            "win_rate":    round(win / len(rets) * 100, 1),
            "avg_return":  round(sum(rets) / len(rets), 2),
            "best":        round(max(rets), 2),
            "worst":       round(min(rets), 2),
            "occurrences": occurrences[-15:],
        }
    cache_set(cache_key, result)
    return result


# ============================================================================
# 3) AI commentary (Gemini)
# ============================================================================
def _get_gemini_key() -> str:
    cfg = load_json(GEMINI_FILE, {})
    return (cfg.get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")).strip()


class GeminiReq(BaseModel):
    api_key: str


@app.post("/api/gemini")
def api_gemini_set(req: GeminiReq):
    save_json(GEMINI_FILE, {"api_key": req.api_key.strip()})
    return {"ok": True}


@app.get("/api/gemini")
def api_gemini_get():
    return {"configured": bool(_get_gemini_key())}


@app.get("/api/ai-comment/{code}")
def api_ai_comment(code: str):
    """用 Gemini 對單檔產生 3-5 句中文評論。"""
    key = _get_gemini_key()
    if not key:
        return {"ok": False, "msg": "尚未設定 GEMINI API key（從工具列「🤖 AI」按鈕設定）"}

    cache_key = f"ai:{code}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    try:
        d = fetch_stock(code)
    except Exception as e:
        raise HTTPException(503, str(e))

    portfolio = load_portfolio()  # list (multi-entry)
    holdings_of_code = [h for h in portfolio if h.get("code") == code]

    sigs = "、".join(s["label"] for s in d.get("signals", [])) or "無強烈訊號"

    # 內部人交易具體數字 (近 6 個月)
    insider_line = ""
    try:
        ins = fetch_insider(code, days=180)
        s = ins["summary"]
        if s.get("total_count", 0) > 0:
            net = s["net_value"]
            net_str = f"+${net/1e6:.1f}M" if net >= 0 else f"-${abs(net)/1e6:.1f}M"
            insider_line = (f"\n內部人 6 個月：淨值 {net_str}"
                            f"（買 {s['buy_count']} 筆 ${s['buy_value']/1e6:.1f}M, "
                            f"賣 {s['sell_count']} 筆 ${s['sell_value']/1e6:.1f}M）")
    except Exception:
        pass

    # 13F 機構持股共識度
    inst_line = ""
    try:
        ih = fetch_institutional_holders(code)
        isum = ih["summary"]
        if isum.get("pct_institutions", 0) > 0:
            top_holder = ih["institutional"][0]["holder"] if ih.get("institutional") else "—"
            inst_line = (f"\n13F 機構共識：總機構 {isum['pct_institutions']}% / "
                         f"Top 10 集中 {isum['top10_pct']}% / 內部人 {isum['pct_insider']}% "
                         f"(最大持有: {top_holder[:30]})")
    except Exception:
        pass

    # 使用者持股 + 移動停利建議
    pos = ""
    if holdings_of_code:
        total_shares = sum(float(h.get("shares", 0)) for h in holdings_of_code)
        total_cost   = sum(float(h.get("shares", 0)) * float(h.get("cost_price", 0))
                           for h in holdings_of_code)
        avg_cost = total_cost / total_shares if total_shares > 0 else 0
        ret = (d["price"] - avg_cost) / avg_cost * 100 if avg_cost else 0
        n = len(holdings_of_code)
        trail = _trailing_stop_advice(d.get("yf", code), "", avg_cost, d["price"])
        pos = (f"\n使用者持股：{total_shares} 股 ({n} 筆)，平均成本 ${avg_cost:.2f}，"
               f"損益 {ret:+.2f}%，移動停利建議：{trail.get('rule','—')}")

    prompt = f"""你是美股技術分析助理。用 4-6 句繁體中文評論以下個股，最後給「短線操作建議」一句話。
請避免免責聲明、不要列點，直接給結論。評論時請綜合技術面 + 籌碼面 (內部人 + 13F 機構)。

【{d['name']} ({d['code']}) {d['tag']}】
收盤 ${d['price']}（前日 ${d['prev']}, {(d['price']-d['prev'])/d['prev']*100:+.2f}%）
趨勢：{d['trend']}, 均線：{d['maStatus']}（5/20/60 = {d['ma5']}/{d['ma20']}/{d['ma60']}）
RSI(14) = {d['rsi']}, KD(9,3) K/D = {d['kd_k']}/{d['kd_d']}, MACD {d['macd']}
量能變化 {d['volChange']:+.1f}%（5 日均量 {d['avgVol']:,} 股）
分析師評等：Strong Buy 累計 {d['chip']['fi_10']} 家、Buy {d['chip']['it_10']} 家
近期訊號：{sigs}
壓力 ${d['resist']} / 支撐 ${d['support']}{insider_line}{inst_line}{pos}
"""
    try:
        text = _gemini_call(key, prompt)
    except Exception as e:
        return {"ok": False, "msg": f"Gemini 失敗: {e}"}

    out = {"ok": True, "code": code, "comment": text, "asOf": d["asOf"]}
    cache_set(cache_key, out)
    return out


# ============================================================================
# 4) Group heatmap (族群熱度地圖)
# ============================================================================
@app.get("/api/group-heatmap")
def api_group_heatmap():
    from concurrent.futures import ThreadPoolExecutor
    wl = load_watchlist()

    def safe(code):
        try:
            return fetch_summary(code)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = [r for r in ex.map(safe, wl.keys()) if r and "error" not in r]

    groups: dict[str, list] = {}
    for s in results:
        groups.setdefault(s.get("group", "自選"), []).append(s)

    out = []
    for g, members in groups.items():
        changes = [(m["price"] - m["prev"]) / m["prev"] * 100 for m in members if m.get("prev")]
        avg = sum(changes) / len(changes) if changes else 0
        wins = sum(1 for c in changes if c > 0)
        members_sorted = sorted(members, key=lambda x: -((x["price"] - x["prev"]) / x["prev"] * 100 if x.get("prev") else 0))
        out.append({
            "group":      g,
            "count":      len(members),
            "avg_change": round(avg, 2),
            "wins":       wins,
            "losses":     len(changes) - wins,
            "max_up":     round(max(changes), 2) if changes else 0,
            "max_down":   round(min(changes), 2) if changes else 0,
            "members": [{
                "code":   m["code"],
                "name":   m["name"],
                "price":  m["price"],
                "change": round((m["price"] - m["prev"]) / m["prev"] * 100, 2) if m.get("prev") else 0,
            } for m in members_sorted],
        })
    out.sort(key=lambda x: -x["avg_change"])
    return out


# ============================================================================
# 5) Fundamentals (季營收 + 季 EPS via yfinance.quarterly_income_stmt)
# ============================================================================
@app.get("/api/fundamentals/{code}")
def api_fundamentals(code: str):
    """從 yfinance 抓季營收 + 季 EPS（美股按季公布）。"""
    cached = cache_get(f"fund:{code}")
    if cached:
        return cached

    wl = load_watchlist()
    info = wl.get(code)
    if not info:
        raise HTTPException(404)
    yf_code = info["yf"]

    revenue: list[dict] = []
    eps: list[dict] = []
    try:
        t = yf.Ticker(yf_code)
        qis = t.quarterly_income_stmt
        if qis is not None and not qis.empty:
            cols = sorted(qis.columns)  # 由舊到新
            qis = qis[cols]
            # Total Revenue
            row_rev = None
            for key in ("Total Revenue", "Operating Revenue", "Revenue"):
                if key in qis.index:
                    row_rev = qis.loc[key]
                    break
            if row_rev is not None:
                # YoY: 對齊 4 季前
                vals = [float(v) if not pd.isna(v) else 0.0 for v in row_rev.values]
                for i, dt in enumerate(cols):
                    if vals[i] <= 0:
                        continue
                    yoy = None
                    if i >= 4 and vals[i - 4] > 0:
                        yoy = round((vals[i] - vals[i - 4]) / vals[i - 4] * 100, 1)
                    quarter = (dt.month - 1) // 3 + 1
                    revenue.append({
                        "ym":      f"{dt.year}/Q{quarter}",
                        "revenue": int(vals[i]),     # USD ($)
                        "yoy":     yoy,
                    })
                revenue = revenue[-12:]
            # Basic / Diluted EPS
            row_eps = None
            for key in ("Diluted EPS", "Basic EPS"):
                if key in qis.index:
                    row_eps = qis.loc[key]
                    break
            if row_eps is not None:
                for dt, v in zip(cols, row_eps.values):
                    if pd.isna(v):
                        continue
                    eps.append({
                        "date":  dt.strftime("%Y-%m-%d"),
                        "value": round(float(v), 2),
                    })
                eps = eps[-12:]
    except Exception as e:
        print(f"[fund] {code}: {e}")

    out = {"code": code, "revenue": revenue, "eps": eps}
    cache_set(f"fund:{code}", out)
    return out


# ============================================================================
# 6) Index overlay (S&P 500 大盤連動)
# ============================================================================
@app.get("/api/index")
def api_index(period: str = "D"):
    """S&P 500 ^GSPC，回傳跟個股對齊用的 normalized 線。"""
    period = period.upper() if period.upper() in PERIOD_CFG else "D"
    cache_key = f"gspc:{period}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    cfg = PERIOD_CFG[period]
    try:
        h = yf.Ticker("^GSPC").history(period=cfg["period"], interval=cfg["interval"], auto_adjust=False)
    except Exception as e:
        return {"dates": [], "close": [], "error": str(e)}
    h = h.dropna(subset=["Close"]).iloc[-cfg["n"]:]
    if h.empty:
        return {"dates": [], "close": []}

    base = float(h["Close"].iloc[0])
    out = {
        "dates":   [idx.strftime("%m/%d") if period == "D" else idx.strftime("%Y/%m" if period == "M" else "%m/%d") for idx in h.index],
        "close":   [round(float(c), 2) for c in h["Close"].tolist()],
        "norm":    [round(float(c) / base * 100, 2) for c in h["Close"].tolist()],  # 起點 = 100
        "current": round(float(h["Close"].iloc[-1]), 2),
        "prev":    round(float(h["Close"].iloc[-2]), 2) if len(h) > 1 else round(base, 2),
    }
    cache_set(cache_key, out)
    return out


# ============================================================================
# 7) Backtest engine
# ============================================================================
@app.get("/api/backtest")
def api_backtest(
    strategy:    str   = "momentum",   # momentum | meanrev | win_rate | group | hot_group
    start:       str   = "",
    end:         str   = "",
    capital:     float = 100_000.0,
    hold_days:   int   = 5,
    n_positions: int   = 5,
    threshold:   float = 65,           # for win_rate
    group:       str   = "七巨頭",
    universe:    str   = "watchlist",  # 'watchlist' | 'group:七巨頭' | ...
):
    """每日 simulate 的簡化回測引擎。
    策略：
      momentum  – 每日買漲幅前 N
      meanrev   – 每日買跌幅前 N
      win_rate  – 短線勝率 >= threshold 的所有股 (取 N 檔)
      group     – 都買指定族群
      hot_group – 都買當日漲幅最高的族群
    持股 hold_days 天後賣出，等比配重，benchmark = ^GSPC。
    """
    if not start:
        start = (pd.Timestamp.today() - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    if not end:
        end = pd.Timestamp.today().strftime("%Y-%m-%d")

    wl = load_watchlist()
    if universe.startswith("group:"):
        g = universe.split(":", 1)[1]
        codes = [c for c, m in wl.items() if (m.get("group") or "") == g]
    elif universe == "magseven":
        codes = [c for c, m in wl.items() if (m.get("group") or "") == "七巨頭"]
    else:
        codes = list(wl.keys())
    if not codes:
        raise HTTPException(400, "Universe is empty")

    yf_codes = [wl[c]["yf"] for c in codes]
    cache_key = f"bt:{','.join(yf_codes)}:{start}:{end}"
    closes = None
    cached_prices = cache_get(cache_key)
    if cached_prices is not None:
        closes = cached_prices
    else:
        try:
            data = yf.download(yf_codes, start=start, end=end, auto_adjust=False,
                               progress=False, group_by="ticker", threads=True)
        except Exception as e:
            raise HTTPException(503, f"yfinance download fail: {e}")
        closes = pd.DataFrame()
        for c, yfc in zip(codes, yf_codes):
            try:
                col = data[yfc]["Close"] if len(yf_codes) > 1 else data["Close"]
                closes[c] = col
            except (KeyError, AttributeError):
                continue
        closes = closes.dropna(how="all")
        _cache[cache_key] = (time.time() + 1800 - CACHE_TTL, closes)  # 30 min cache

    if closes.empty or len(closes) < hold_days + 5:
        raise HTTPException(503, "歷史資料不足")

    try:
        bench_raw = yf.download("^GSPC", start=start, end=end, auto_adjust=False, progress=False)
        bench = bench_raw["Close"]
        if hasattr(bench, 'columns'):  # 有時是 DataFrame 不是 Series
            bench = bench.iloc[:, 0]
    except Exception:
        bench = None

    def signals_at_day(day_idx: int) -> list:
        if day_idx < 20:
            return []
        today_close = closes.iloc[day_idx]
        prev_close  = closes.iloc[day_idx - 1]
        day_returns = ((today_close - prev_close) / prev_close * 100).dropna()
        if day_returns.empty:
            return []

        if strategy == "momentum":
            return day_returns.nlargest(n_positions).index.tolist()
        if strategy == "meanrev":
            return day_returns.nsmallest(n_positions).index.tolist()
        if strategy == "win_rate":
            picks = []
            for c in closes.columns:
                hist = closes[c].iloc[:day_idx+1].dropna()
                if len(hist) < 20: continue
                ma5  = hist.iloc[-5:].mean()
                ma20 = hist.iloc[-20:].mean()
                delta = hist.diff().iloc[-14:].dropna()
                gain = delta[delta > 0].sum()
                loss = -delta[delta < 0].sum()
                rsi = 100 - 100/(1 + (gain/loss)) if loss > 0 else 100
                base = 50
                if ma5 > ma20: base += 15
                elif ma5 < ma20: base -= 15
                if rsi > 70: base -= 10
                elif rsi < 30: base += 10
                if base >= threshold:
                    picks.append((c, base))
            picks.sort(key=lambda x: -x[1])
            return [p[0] for p in picks[:n_positions]]
        if strategy == "group":
            return [c for c in closes.columns if (wl.get(c, {}).get("group") or "") == group]
        if strategy == "hot_group":
            grp_returns = {}
            for c in day_returns.index:
                g = wl.get(c, {}).get("group", "其他")
                grp_returns.setdefault(g, []).append(day_returns[c])
            if not grp_returns: return []
            avgs = {g: sum(v)/len(v) for g, v in grp_returns.items() if v}
            best_g = max(avgs, key=avgs.get)
            return [c for c in closes.columns if (wl.get(c, {}).get("group") or "") == best_g]
        return []

    initial   = capital
    cash      = capital
    positions = {}
    equity_curve = []
    trades = []

    dates = closes.index.tolist()
    for day_idx, dt in enumerate(dates):
        today_close = closes.iloc[day_idx]
        # Close positions whose hold period reached
        for code in list(positions.keys()):
            pos = positions[code]
            if day_idx - pos["entry_idx"] >= hold_days:
                exit_p = today_close.get(code)
                if exit_p is None or pd.isna(exit_p):
                    continue
                exit_p = float(exit_p)
                cash += pos["shares"] * exit_p
                trades.append({
                    "code":       code,
                    "open_date":  pos["entry_date"],
                    "close_date": dt.strftime("%Y-%m-%d"),
                    "entry":      round(pos["entry_price"], 2),
                    "exit":       round(exit_p, 2),
                    "shares":     round(pos["shares"], 2),
                    "pnl":        round((exit_p - pos["entry_price"]) * pos["shares"], 0),
                    "ret_pct":    round((exit_p - pos["entry_price"]) / pos["entry_price"] * 100, 2),
                })
                del positions[code]

        # Open new positions
        if len(positions) < n_positions:
            buys = signals_at_day(day_idx)
            for code in buys:
                if code in positions: continue
                if len(positions) >= n_positions: break
                price = today_close.get(code)
                if price is None or pd.isna(price): continue
                price = float(price)
                if price <= 0: continue
                slot_value = min(capital / n_positions, cash)
                if slot_value < 100: continue
                shares = slot_value / price
                cash -= shares * price
                positions[code] = {
                    "entry_date":  dt.strftime("%Y-%m-%d"),
                    "entry_idx":   day_idx,
                    "shares":      shares,
                    "entry_price": price,
                }

        # Mark-to-market
        mv = 0.0
        for code, pos in positions.items():
            p = today_close.get(code)
            if p is None or pd.isna(p): p = pos["entry_price"]
            mv += pos["shares"] * float(p)
        equity = cash + mv

        bench_v = None
        if bench is not None and len(bench) > 0:
            try:
                base_b = float(bench.iloc[0])
                cur_b = float(bench.loc[dt]) if dt in bench.index else None
                if cur_b and base_b:
                    bench_v = round(cur_b / base_b * initial, 0)
            except Exception:
                bench_v = None

        equity_curve.append({
            "date":      dt.strftime("%Y-%m-%d"),
            "equity":    round(equity, 0),
            "benchmark": bench_v,
        })

    # Summary
    if equity_curve:
        final_eq = equity_curve[-1]["equity"]
        total_ret = (final_eq - initial) / initial * 100
        peak, mdd = initial, 0
        for e in equity_curve:
            peak = max(peak, e["equity"])
            dd = (e["equity"] - peak) / peak * 100
            mdd = min(mdd, dd)
        bench_final = equity_curve[-1].get("benchmark")
        bench_ret = ((bench_final - initial) / initial * 100) if bench_final else None
        winning = sum(1 for t in trades if t["ret_pct"] > 0)
        wr = round(winning / len(trades) * 100, 1) if trades else 0
        avg_win = round(sum(t["ret_pct"] for t in trades if t["ret_pct"] > 0) / max(winning, 1), 2)
        loser_cnt = len(trades) - winning
        avg_loss = round(sum(t["ret_pct"] for t in trades if t["ret_pct"] <= 0) / max(loser_cnt, 1), 2)
    else:
        final_eq, total_ret, mdd, bench_ret, wr, avg_win, avg_loss = initial, 0, 0, None, 0, 0, 0

    # Downsample equity curve if too long
    step = max(1, len(equity_curve) // 250)
    ec_ds = equity_curve[::step]
    if equity_curve and ec_ds[-1] != equity_curve[-1]:
        ec_ds.append(equity_curve[-1])

    return {
        "strategy": strategy,
        "params": {
            "start": start, "end": end, "capital": initial,
            "hold_days": hold_days, "n_positions": n_positions,
            "threshold": threshold, "group": group, "universe": universe,
        },
        "summary": {
            "start_date":       dates[0].strftime("%Y-%m-%d") if dates else "",
            "end_date":         dates[-1].strftime("%Y-%m-%d") if dates else "",
            "trading_days":     len(dates),
            "initial_capital":  initial,
            "final_equity":     final_eq,
            "total_return":     round(total_ret, 2),
            "benchmark_return": round(bench_ret, 2) if bench_ret is not None else None,
            "alpha":            round(total_ret - bench_ret, 2) if bench_ret is not None else None,
            "max_drawdown":     round(mdd, 2),
            "win_rate":         wr,
            "n_trades":         len(trades),
            "avg_win":          avg_win,
            "avg_loss":         avg_loss,
        },
        "equity_curve": ec_ds,
        "trades": trades[-100:],
    }


@app.get("/")
def root():
    return FileResponse(ROOT / "index.html")


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, io
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    import uvicorn
    print("=" * 64)
    print("[US Stock Intel v1]  http://localhost:18506/")
    print("    GET  /api/stocks                    清單摘要")
    print("    GET  /api/stock/{code}?period=D|W|M  詳細")
    print("    GET  /api/news/{code}                新聞")
    print("    GET  /api/groups                     族群")
    print("    GET  /api/ranking?by=change|volume|fi|rsi 熱度榜")
    print("    POST /api/watchlist                  新增")
    print("    DEL  /api/watchlist/{code}           移除")
    print("    POST /api/alerts/{code}              設警示")
    print("    POST /api/telegram                   設 bot")
    print("    GET  /api/refresh                    清快取")
    print("=" * 64)
    threading.Thread(target=alert_worker, daemon=True).start()
    uvicorn.run("server:app", host="0.0.0.0", port=18506, reload=False)
