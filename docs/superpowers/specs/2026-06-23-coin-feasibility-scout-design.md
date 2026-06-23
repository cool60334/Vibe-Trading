# 幣種可行性偵察器（Coin Feasibility Scout）— 設計

- **日期**：2026-06-23
- **分支**：quant-trading-dashboard
- **狀態**：設計已核准，待寫實作計畫
- **優先級脈絡**：P1（擴幣種，三大方向之首）。本 spec 只涵蓋「偵察器」這個 enabler，不含實際 onboard / 批次跑 / 跨幣排名。

---

## 1. 背景與動機

研究 pipeline 目前只跑 btc / eth / sol。要找其他幣種的可部署 alpha，加一個幣只需在 `research/research_config.yaml` 加 3 行（`name` / `okx_swap` / `ccxt_bybit`，其中 `binance_usdt` 由 `SymbolConfig` 自動推導為 `{NAME}USDT`）。pipeline 是 symbol-loop 驅動，sol 已證明新幣能端到端跑通。

**問題**：沒有任何「跑之前先確認資料夠不夠」的工具。風險：

1. 在一個沒有 Binance OI archive 覆蓋、或歷史太短的幣上跑完整 pipeline（數小時），結果持倉因子（`ls_divergence` 等 —— 目前唯一自動建出 selected 候選 sol_s1 的 edge）靜默缺席，候選集很弱。
2. OKX 永續上市較晚的幣，OHLCV / funding 歷史不足 train+OOS，回測沒意義。

**結論**：先做一個確定性偵察器，探測每個候選幣在 pipeline 三個免費資料源的覆蓋深度，吐 go/no-go 報告。只 onboard 過關的幣。

### pipeline 依賴的三個免費資料源

| 源 | 用途 | lib |
|---|---|---|
| OKX OHLCV | stage0a 主資料（價量技術指標） | `research/lib/okx_data.py::fetch_candles` |
| ccxt Binance funding | `funding_z` 因子 | `research/lib/ccxt_data.py::fetch_funding_rate_history_ccxt`（stage0a 實際用的源；OKX public 端點卡 ~99d 不用） |
| Binance daily-metrics archive | OI + 持倉因子（`global_ls_acct_z` / `toptrader_ls_z` / `ls_divergence` / `taker_buysell_ratio`） | `research/lib/binance_dump.py::metrics_url` + `oi_metrics.py` |

> Bybit OI 只是 archive 缺席時的 fallback，且不提供持倉因子 → **偵察器不探測 Bybit**（只在輸出的 config 片段填推導值）。

---

## 2. 目標 / 非目標

### 目標
- 給一組候選幣（CLI 參數，預設 = 保守 6 幣 BNB/XRP/DOGE/ADA/LTC/BCH），探測三源覆蓋。
- 對每個幣判 GO / PARTIAL / NO_GO，附理由與實際最早日期 / 深度。
- 輸出機器可讀報告 `research/manifests/coin_scout.json` + 人可讀主控台表格 + GO 幣的可貼 YAML 片段。
- 唯讀、便宜（只探「存在性 + 最早日期」，不全量下載）、確定性、可重跑、可測（網路探測可 mock）。

### 非目標（YAGNI）
- 自動編輯 `research_config.yaml`（偵察器只印片段，由人貼上 —— 較安全）。
- 跨幣結果排名 / dashboard 視圖（延後，視 onboard 後需求再開）。
- Bybit 覆蓋探測（fallback-only）。
- 完整資料 backfill / 迷你 stage0a 試跑。

---

## 3. 設計

### 3.1 模組拆分（隔離、可測）

**`research/lib/coin_scout.py`** —— 純邏輯 + 探測協調：

資料類（frozen dataclass）：
```
SourceCoverage(available: bool, earliest: date | None, depth_days: int | None,
               ok: bool, error: str | None)
CoinVerdict(name: str, okx_swap: str, ccxt_bybit: str, binance_usdt: str,
            okx_ohlcv: SourceCoverage, funding: SourceCoverage,
            binance_archive: SourceCoverage,
            verdict: str,            # "GO" | "PARTIAL" | "NO_GO"
            reasons: list[str])
ScoutReport(generated_at: str, thresholds: dict, coins: list[CoinVerdict])
```

函式：
- `probe_okx_ohlcv(okx_swap) -> SourceCoverage`
- `probe_funding(ccxt_symbol) -> SourceCoverage`（ccxt Binance 多年；非 OKX 99d）
- `probe_binance_archive(binance_usdt) -> SourceCoverage`
- `score_coin(ohlcv, funding, archive, thresholds) -> (verdict, reasons)` —— **純函式，重點單元測試對象**
- `scout_coins(names, thresholds) -> ScoutReport` —— 協調：對每個幣推 ticker、跑三探測、score、組報告
- `report_to_json(report) -> dict`、`format_table(report) -> str`、`go_coins_yaml(report) -> str`

**`research/pipeline/coin_scout.py`** —— 薄 CLI driver：
- `--coins bnb,xrp,doge`（逗號分隔；省略 = 預設保守 6 幣常數 `DEFAULT_CANDIDATES`）
- 從幣名推 ticker：`okx_swap = f"{NAME}-USDT-SWAP"`、`ccxt_bybit = f"{NAME}/USDT:USDT"`、`binance_usdt = f"{NAME}USDT"`（與 `SymbolConfig` 一致）
- 呼叫 `scout_coins` → 寫 `research/manifests/coin_scout.json` → 印表格 + GO 幣 YAML
- `--out <path>` 可選覆寫輸出路徑

> 網路 I/O 全部關在 probe 函式裡（且 probe 內呼叫既有 `okx_data` / `binance_dump`）；`score_coin` 等純邏輯零網路 → 測試用 monkeypatch 把三個 probe 換成 stub。

### 3.2 探測機制（唯讀，不全量下載）

- **OKX OHLCV 最早**：呼叫 `fetch_candles(okx_swap, days=≈2000, interval="1H")`，它本來就會分頁回拉到 cutoff；取回傳 frame 的最舊 index → `earliest`，`depth_days = (today - earliest).days`。空 frame / 例外 → `available=False`。
- **funding 最早**：`ccxt_data.fetch_funding_rate_history_ccxt(exchange_name="binance", symbol=ccxt_symbol)` 取最舊（鏡像 stage0a；OKX public funding-rate-history 卡 ~99d 故不用）。
- **Binance archive 最早**：對 `metrics_url(binance_usdt, day)` 做 HTTP **HEAD**（200=存在、404=無）。在 `[date(2020,1,1), today - 2d]` 二分搜最早存在日（上界減 2 天避開 T+1 尚未發布）。`earliest` = 最早 200 的日子。全程無 200 → `available=False`。
  - 新增輕量 helper `metrics_day_exists(symbol, day) -> bool`（HEAD 探測）放 **`binance_dump.py`**（網路 I/O 集中於此 lib，與 `metrics_url` / `_fetch` 同檔）。

每個 probe 包 try/except：乾淨 404 / 空資料 → `available=False, error=None`；網路例外（timeout/連線）→ `available=False, error="<訊息>"`、**不中斷**整輪，繼續下一探測 / 下一幣。

### 3.3 判決規則（門檻 = 模組常數，可改）

常數（`coin_scout.py`）：
```
MIN_OHLCV_DAYS   = 365   # 低於此 → NO_GO（連回測都不夠）
TARGET_DEPTH_DAYS = 730  # 三源皆 ≥ 此 → GO
CONFIG_PERIOD_DAYS = 1460  # 只作報告提示：現有幣 4yr，淺幣 OOS 較短
```

`score_coin` 邏輯（純、確定性）：
1. OKX OHLCV 不可用 或 `depth < MIN_OHLCV_DAYS` → **NO_GO**（理由帶實際深度）。
2. 否則若 `ohlcv.depth ≥ TARGET` 且 `funding.depth ≥ TARGET` 且 `archive 可用且 depth ≥ TARGET` → **GO**。
3. 其餘 → **PARTIAL**（理由標明缺哪源 / 哪源太淺；特別標 archive 缺 = 失去持倉因子）。

理由字串範例：`"binance_archive missing -> no positioning factors"`、`"okx_ohlcv depth 540d < target 730d"`、`"all sources >= 730d (ohlcv 1500d, funding 1500d, archive 1400d)"`。報告永遠帶實際深度，讓人可覆寫判斷。

### 3.4 輸出

**`research/manifests/coin_scout.json`**：
```json
{
  "generated_at": "2026-06-23T...Z",
  "thresholds": {"min_ohlcv_days": 365, "target_depth_days": 730, "config_period_days": 1460},
  "coins": [
    {"name": "bnb", "okx_swap": "BNB-USDT-SWAP", "ccxt_bybit": "BNB/USDT:USDT",
     "binance_usdt": "BNBUSDT",
     "okx_ohlcv": {"available": true, "earliest": "2020-02-10", "depth_days": 2295, "ok": true, "error": null},
     "funding": {...}, "binance_archive": {...},
     "verdict": "GO", "reasons": ["all sources >= 730d (...)"]}
  ]
}
```

**主控台表格**：
```
coin  okx_ohlcv  funding  binance_archive  verdict
bnb   2295d      2295d        1400d            GO
xrp   ...        ...          ...              PARTIAL
```

**GO 幣 YAML 片段**（可直接貼進 `research_config.yaml` 的 `symbols:`）：
```yaml
- name: bnb
  okx_swap: "BNB-USDT-SWAP"
  ccxt_bybit: "BNB/USDT:USDT"
```

### 3.5 邊界情形

- 整源網路掛 → 該源 `available=False, error=msg`、判決不因單一探測崩；繼續其他幣。
- Binance archive 昨天檔未發布 → 二分上界用 `today - 2d`。
- OKX 深拉分頁多次 → 用既有 `fetch_candles` 的 cutoff 停止，給 `days≈2000`（≈5.5yr）已足涵蓋最老幣。
- 幣名大小寫 → 一律以小寫存、ticker 推導用大寫，與 `SymbolConfig.prefix` / `.binance_usdt` 一致。

---

## 4. 測試（TDD）

- **單元（純邏輯，無網路）**：
  - `score_coin`：GO / PARTIAL / NO_GO 三類各 + 邊界（depth 剛好 = 365 / 730）、archive 缺 → PARTIAL、ohlcv 缺 → NO_GO、funding 太淺 → PARTIAL。
  - 二分搜最早日：mock `metrics_day_exists`，驗最早邊界、全 False、全 True、單點。
  - `report_to_json` / `format_table` / `go_coins_yaml` 序列化正確。
  - ticker 推導正確（大小寫）。
- **CLI**：`--coins` 解析、預設清單、`--out` 覆寫（mock `scout_coins`）。
- **probe 函式**：monkeypatch 掉 `okx_data` / HEAD，驗回傳 `SourceCoverage`（含 error 路徑）。測試**不打真網路**。
- **手動 live smoke（非自動）**：實跑預設 6 幣一次，肉眼確認最早日期合理（BNB/XRP 應 GO 且深度數年）。

> 註（來自 [[backlog-pipeline-minor-cleanups]] 教訓）：research/tests 與 dashboard/server 的 pytest **必須分開跑**，合跑有 sys.path 衝突。本 spec 只動 research/ 故跑 research 套件即可。

---

## 5. 成功標準

跑 `python -m research.pipeline.coin_scout` →

1. 對保守 6 幣（BNB/XRP/DOGE/ADA/LTC/BCH）各印一列判決，深度數字與直覺相符（老牌幣應 GO）。
2. 產出 `coin_scout.json` 結構合法。
3. GO 幣印出可直接貼的 YAML。
4. 新增 research 測試全綠、零打真網路。

→ 我據此把 GO 幣 onboard 進 `research_config.yaml`，再批次跑 pipeline（onboard / 批跑 = 本 spec 之外的後續執行）。

---

## 6. 已定決策（實跑後可微調，不阻塞實作）

- `metrics_day_exists` 放 `binance_dump.py`（網路 I/O 集中）。
- 預設候選清單寫死於 CLI 常數 `DEFAULT_CANDIDATES`（YAGNI，不讀 config 區塊）。
- 門檻常數採 365 / 730 / 1460；實跑 6 幣後若太鬆/嚴再調（報告含實際深度，可人工覆寫）。
