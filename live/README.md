# Live Alert-Only MVP

無下單版本。每小時拉資料 → 算訊號 → log → console alert。本機跑無風險，目的：在進 Bybit testnet 前驗證 live 訊號 = backtest 訊號（layer 1 驗證）。

## Files

| File | Purpose |
|------|---------|
| `alert_only.py` | 主執行腳本，每小時跑一次 |
| `check_parity.py` | 驗證 live log == backtest engine 輸出 |
| `state/signals.parquet` | 訊號 log（自動建立，gitignored）|

## Manual run

```powershell
cd C:\Users\cool6\Vibe-Trading
python live\alert_only.py
```

每次 invocation：
1. 讀 `state/signals.parquet`（如不存在則建立）
2. 拉最近 130 天 funding (Binance) + F&G (alternative.me) + BTC OHLCV (1h + 1d)
3. 算 regime + ensemble (S2+S3+S4 short-only) + 48/2 persistence + gate
4. Append 比 log 最後一筆更晚的所有小時 → `state/signals.parquet`
5. 印當下最新訊號（含 vs 前次 position 變化）

## Catch-up 邏輯

關機 N 小時後開機跑 → script 自動補 N 小時：因為訊號 deterministic，拉同樣歷史資料 = 算出同樣訊號。

不需任何特別處理，每次跑都是 stateless：讀 log → 拉資料 → 算 → 寫新行。

## Webhook（可選）

position 改變時 POST JSON 給 webhook：

```powershell
python live\alert_only.py --webhook https://your-server.example.com/alert
```

Payload 格式（JSON）：
```json
{
  "timestamp": "2026-05-18T13:00:00",
  "regime": "bear",
  "position": -0.333,
  "prev_position": 0.0,
  "close": 77100.0,
  "funding": 0.000058,
  "fng": 28,
  "s2": -1.0,
  "s3": -1.0,
  "s4": 0.0
}
```

## Windows Task Scheduler 設定

每小時整點觸發：

1. 開 Task Scheduler（搜尋「工作排程器」）
2. **Create Task**（非 Basic）
3. **General**:
   - Name: `BTC Alert Only`
   - "Run whether user is logged on or not"（你不必登入）
   - "Run with highest privileges" 勾
4. **Triggers** → New:
   - Begin: On a schedule
   - Daily, 每 1 小時重複，無限期
   - Start time: 任何整點
5. **Actions** → New:
   - Action: Start a program
   - Program: `C:\Users\cool6\AppData\Local\Programs\Python\Python311\python.exe`
   - Arguments: `C:\Users\cool6\Vibe-Trading\live\alert_only.py`
   - Start in: `C:\Users\cool6\Vibe-Trading`
6. **Conditions**:
   - 取消「Start the task only if the computer is on AC power」（讓筆電插電/電池都跑）
7. **Settings**:
   - "If the task fails, restart every: 5 minutes, up to 3 times"
   - "If the running task does not end when requested, force it to stop"

存檔 → 輸入密碼。立即測試：右鍵 task → Run → 看 `state/signals.parquet` 是否新增。

### Cron（Linux/VPS）

```cron
# /etc/cron.d/btc-alert
0 * * * * cool6 cd /home/cool6/Vibe-Trading && /usr/bin/python3 live/alert_only.py >> /var/log/btc-alert.log 2>&1
```

## Parity check（layer 1 驗證）

跑 ≥ 24 小時累積 log 後執行：

```powershell
python live\check_parity.py --days 14
```

對比近 14 天 live log vs 重跑 backtest engine 結果。輸出範例：

```
[parity] 336 aligned hours  match=100.00%  mismatches=0  max_diff=0.000000
[parity] ✅ live signal == backtest signal across 336 hours
```

**通過條件**: match >= 95%（小數值差容忍）+ 無 systematic bias。

任一 mismatch → 印前 5 個衝突小時，debug。常見原因：
- F&G API 抓的最新日期 vs backtest fixture 的 timezone 邊界
- Funding rate 最後一筆未結算 vs 已結算
- BTC OHLCV 最新 1H bar 未收盤（重跑會更新）

## 監控

每天看一眼：

```powershell
python -c "import pandas as pd; df = pd.read_parquet('live/state/signals.parquet'); print(df.tail(24))"
```

或寫個小 dashboard（streamlit / plotly）顯示 position over time。

## 下階段（layer 2: testnet）

Alert-only 訊號驗 1 週通過 → 改寫 `alert_only.py` 加 ccxt.bybit() testnet 下單模組：
- 讀當下 target position
- ccxt 讀真實倉位
- diff → place_order 調整
- log order_id + filled price

該階段必上 VPS（24/7、IP 穩定、API 鎖）。

## 排錯

| 症狀 | 可能原因 |
|------|---------|
| `fetch_funding_rate_history error` | Binance API rate limit、改 `time.sleep(0.5)` |
| `F&G 抓不到` | alternative.me 偶爾 503，retry |
| `position` 永遠 0 | regime 不是 bear（現在 bull 中正常）|
| Task Scheduler 不執行 | "Run whether user logged on" 沒勾、密碼錯 |
| `KeyError: 'fng'` | F&G API 改格式，看 sentiment.py 抓的 JSON |
