# 美股情報站 · US Stock Intelligence Console

一頁式美股儀表板：即時行情、技術指標、分析師評等、訊號偵測、AI 評論、個人持股、Telegram 警示。**英文新聞自動翻譯成繁體中文**。

```
FastAPI + yfinance + Gemini + ECharts
```

## 主要功能

- **即時資料**：yfinance 每日 OHLCV、`Ticker.recommendations` 分析師評等、`Ticker.quarterly_income_stmt` 季財報、`Ticker.news` 英文新聞
- **新聞翻譯**：英文標題自動透過 Gemini 批次翻譯為繁體中文（一次 API call），原文滑鼠 hover 仍可看
- **技術指標**：MA5/20/60、RSI(14)、MACD(12,26,9)、KD(9,3)、W底/M頭、ATR
- **訊號偵測**：黃金/死亡交叉、KD/RSI 超買賣、突破/跌破新高低（日/週/月自動換 lookback）
- **訊號回測**：點訊號徽章看歷史出現次數、勝率、後 5 天平均報酬
- **熱度榜**：6 種排序（漲幅/跌幅/量能/Strong Buy/Sell/RSI）
- **族群熱度地圖**：每族群（七巨頭/半導體/AI 概念）平均漲幅、紅黑比
- **個人持股**：成本/市值/損益/報酬率/權重自動計算（USD）
- **AI 評論**：Gemini 把指標 + 分析師評等 + 訊號 + 持股餵進去產 4-6 句中文評估
- **Telegram 警示**：上下方價位設定，背景每 5 分鐘掃描，突破/跌破自動推播
- **多週期 K 線**：日 / 週 / 月切換 + S&P 500 疊加
- **基本面**：季營收（含 YoY）+ 季 EPS（最近 12 季）

## 啟動

需求：Python 3.9+

```bash
git clone https://github.com/JauWei/us-stock-intel.git
cd us-stock-intel
pip install -r requirements.txt
python server.py
```

開瀏覽器：[http://localhost:18506/](http://localhost:18506/)

> ⚠️ 不能用 `file://` 直接點開 index.html（CORS 會擋）。一定要走 server。

### 從 GitHub Pages 開（前端部份）

如果你把 repo 啟用 GitHub Pages，可以從 `https://你的帳號.github.io/us-stock-intel/` 開前端 UI，**但本機仍需執行 `python server.py`**——前端會自動跨域連到 `localhost:18506`（server 已開 CORS）。

### 📱 手機 / 平板開（與電腦同 WiFi）

兩個方法擇一：

**方法 A：直連 PC LAN IP（推薦，最簡單）**
1. 電腦命令提示字元 `ipconfig` 找 IPv4，例如 `192.168.0.100`
2. Windows 防火牆放行 18506（首次跑 server 會跳「允許私人網路」對話框，按允許）
3. 手機瀏覽器開 `http://192.168.0.100:18506/` — 就能用了
4. 前端會自動偵測「host 帶 :18506」=「就是 server 同源」，不必設定

**方法 B：GitHub Pages 前端 + 手動指定 server URL**
1. 同上 1-2 步驟
2. 手機開 `https://你的帳號.github.io/us-stock-intel/`
3. 點工具列「⚙️ Server」按鈕，填入 `http://192.168.0.100:18506`，點測試 → 儲存
4. 設定會存在手機 `localStorage`，下次自動套用
5. ⚠️ HTTPS 頁面連 HTTP server 部分手機瀏覽器會擋（Mixed Content），若連不上請用方法 A

## 設定（建議全部設）

| 功能 | 怎麼設 | 取得 |
|---|---|---|
| **Gemini 翻譯 + AI 評論** | 工具列「🤖 AI」按鈕貼 API key | [Google AI Studio](https://aistudio.google.com/apikey) 免費 |
| Telegram 警示 | 工具列「🔔 Telegram」貼 bot_token + chat_id | Telegram 找 [@BotFather](https://t.me/BotFather) → /newbot |

> Gemini 同時負責「英文新聞 → 中文標題」翻譯與「AI 個股評論」兩項功能。沒設 key 也能完整使用儀表板，只是新聞會保留英文、AI 評論按鈕會提示尚未設定。

## 預設 16 檔觀察清單

| 族群 | 代號 |
|---|---|
| 七巨頭 (Magnificent 7) | AAPL · MSFT · GOOGL · AMZN · META · NVDA · TSLA |
| 半導體 | AMD · AVGO · TSM · QCOM · ARM |
| AI 概念 | PLTR · SMCI |
| 其他 | NFLX · JPM |

工具列「➕ 新增股票」可以直接加任何美股代號（自動抓 yfinance 名稱與 sector）。

## 與台股版差異

如果你也用 [tw-stock-intel](https://github.com/JauWei/tw-stock-intel)，差異一覽：

| 項目 | 台股版 (port 18505) | 美股版 (port 18506) |
|---|---|---|
| 行情來源 | yfinance（.TW / .TWO） | yfinance（直接代號） |
| 籌碼面 | 三大法人（FinMind） | 分析師評等（yfinance.recommendations） |
| 大盤指數 | ^TWII 加權指數 | ^GSPC S&P 500 |
| 新聞 | Google News 中文 RSS | 英文 + Gemini 翻譯 |
| 基本面 | 月營收（FinMind） | 季營收（yfinance） |
| 持股單位 | 張（×1000 股） | 股（整股） |
| 預設族群 | 半導體/ABF 載板/PCB/AI 伺服器 | 七巨頭/半導體/AI 概念 |

兩個 server 可以同時跑（不同 port）。

## 已知限制

- yfinance 美股延遲約 15 分鐘，不適合當沖即時看盤
- `Ticker.recommendations` 只提供近 4 期（月）累計家數，沒有逐日變化
- 新聞翻譯需 Gemini key，沒設就保持英文
- AI 評論預設 `gemini-2.0-flash-exp`，不可用時自動 fallback `gemini-1.5-flash`

## 授權

MIT — 歡迎 fork、修改、商用。

## Disclaimer

本工具為個人學習與資料整理用途，**不構成投資建議**，自負盈虧。
