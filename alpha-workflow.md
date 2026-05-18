# 加密貨幣永續合約 Alpha 策略發掘流程（Claude MCP + Swarm）

> **此為主檔**。後續更新一律改這份；原 plan（`.claude/plans/claude-mcp-alpha-alpha-unified-shannon.md`）僅保留歷史紀錄不再動。

## 修訂歷程

- **v1（2026-05-13 brainstorm 完成）**：9 階段流程指南，含理論策略 + 回測門檻 + Pine/ccxt 導出路徑
- **v2（2026-05-13 walk-through 後）**：補上實戰教訓
  - LLM 採用：**OpenAI gpt-5（策略生成）+ gpt-5-mini（跑量）**，**非** Anthropic（平台不直連）
  - 確認 factor 分析階段**應走 local Python**（mini 寫 API code 易翻車）
  - 加入 Git workflow + backtest 沙箱坑 + 2 年 ccxt 資料 baseline
  - 加入 backtest-diagnose 反饋迴圈（vibe-trading swarm → Claude 對話 review）
- **v2.1（2026-05-13 主檔遷移）**：v2 補充 A-E 分散併入各階段；本檔成為唯一活檔
- **v3（2026-05-18 跨 regime + ensemble 強制規則）**：通用方法論升級
  - 階段 1 加：單因子 |IC| < 0.10 不可單用、cross-regime 驗證強制 ≥ 3 期
  - 新增階段 2.5 Regime detection 層（任何 regime-conditional 策略必備）
  - 階段 3 加 3 個必跑檢查：cost stress、parameter sweep、OOS walk-forward
  - 階段 5 改評分原則：ensemble > 單套、Pareto sum、asymmetric variant 三版必跑
  - 階段 6 補 TV Pine 限制：無乾淨 funding/FNG feed → 監控用，不執行

---

## Context（為何寫這份）

用戶為量化交易新手，希望用 Claude MCP 工具 + swarm（**gpt-5-mini 跑量、gpt-5 跑策略判斷**）發掘加密貨幣**永續合約**的 alpha 策略。優先以**非價格指標**（資金費率、OI、清算、鏈上、情緒等）擬定策略，產出**保守**而非激進的策略，並能：
1. 輸出為 TradingView 可用腳本（Pine Script）
2. 後續可改為 Bybit 量化執行腳本

用戶要求白話說明專有名詞、邊聊邊 brainstorming，並把零碎想法即時記錄。

---

## 用戶已陳述需求（原始）

- 標的：加密貨幣永續合約（perpetual futures）
- Alpha 來源：**價格之外**的指標優先
- 模型：**OpenAI gpt-5（策略生成）+ gpt-5-mini（跑量）**
- 經驗：新手，需白話講解
- 風格：保守、不激進
- 輸出：TradingView Pine Script、未來改 Bybit 量化
- 流程：先 brainstorm chat → 彙整需求 → 產出流程指南
- 過程中需引導性提問

---

## 平台現況盤點（讀 repo 發現）

### 已有 Skill（非價格指標相關）
| Skill | 用途 | 對應指標 |
|------|------|---------|
| `perp-funding-basis` | 永續資金費率 + 基差 | Funding rate, basis spread |
| `liquidation-heatmap` | 清算熱力圖 | 強平密度 |
| `onchain-analysis` | 鏈上資金流 | 交易所流入流出、巨鯨地址 |
| `crypto-derivatives` | 衍生品 | OI（未平倉量）、PCR |
| `stablecoin-flow` | 穩定幣流向 | USDT/USDC 鑄銷、交易所餘額 |
| `token-unlock-treasury` | 代幣解鎖 | 解鎖時程、財庫變化 |
| `social-media-intelligence` | 社群輿論 | Twitter/Discord 熱度 |
| `sentiment-analysis` | 情緒分析 | Fear & Greed, 新聞情緒 |
| `market-microstructure` | 訂單簿微觀結構 | 買賣盤深度、Imbalance |
| `defi-yield` | DeFi 收益 | TVL、APR |
| `okx-market` | OKX 衍生品 | 多空比、大戶持倉 |

### 已有 Swarm 預設（加密相關）
- `crypto_research_lab` — 加密研究實驗室
- `crypto_trading_desk` — 加密交易桌
- `derivatives_strategy_desk` — 衍生品策略桌 ⚠️（**實為 options/Greeks 用途**，永續勿用）
- `factor_research_committee` — 因子研究委員會
- `ml_quant_lab` — ML 量化實驗室
- `sentiment_intelligence_team` — 情緒情報團隊
- `social_alpha_team` — 社群 alpha 團隊
- `statistical_arbitrage_desk` — 統計套利桌
- `quant_strategy_desk` — 量化策略桌

### 輸出 / 執行
- `pine-script` skill + `/pine` 端點 → 直接導 TradingView Pine v6
- `ccxt` skill → Bybit / Binance / OKX 通用連線
- `vnpy-export` skill → 國產量化框架腳本
- 回測引擎含加密貨幣（crypto） + 統計驗證（Monte Carlo、Bootstrap CI、Walk-Forward）

### 用戶疑問回答
1. **能否輸出 TradingView 腳本？**
   → 可以。平台 `/pine` 端點直接輸出 Pine Script v6，可貼到 TradingView 跑。
2. **後續改 Bybit 可量化腳本？**
   → 可行但**非直譯**。Pine Script 為 TradingView 內部語言，無法在 Bybit 跑單；
   → 需走 `ccxt` 路線：把策略邏輯重寫為 Python + ccxt 包成執行腳本。
   → 平台已有 `vnpy-export` 走類似流程作參考。

---

## Brainstorm 對話記錄

### Q1 持倉時長
**答：A~C 都行**（幾分鐘~7天皆可接受，排除 D 數週以上）
→ 解讀：用戶想保留彈性，不限定單一頻率。策略族群應涵蓋多時間框架。

### Q2 盯盤時間 + 執行方式
**答：A、B 都行**（每日 <2hr，全自動 or 半自動皆可，排除純手動）
→ 解讀：策略必須**規則化**（rule-based），避免主觀判斷型。

### Q3 資金 + 最大回撤
**答：資金 A (<$1,000)，DD B (10%)**
→ 槓桿 1-2x、單筆風險 ≤ 2%、單一策略不分散。

### Q4 指標數據源優先順序
**答：以 Vibe-Trading 平台能取得的數據優先**

**🟢 Tier 1（零摩擦，立刻可用）**：Funding Rate、OI（當下）、F&G、訂單簿、OHLCV
**🟡 Tier 2**：大戶多空比、穩定幣流、鏈上基本指標
**🔴 Tier 3**：清算熱力圖、鯨魚追蹤、Twitter（皆需付費）

### Q5 策略型態
**答：都要**（4 套型態並行回測選最佳）

### Q6.1 ML 探索：**純規則先跑**（v1 不含 ML）
### Q6.2 回測門檻：**Sharpe ≥ 1.5、MaxDD ≤ 10%、Trades ≥ 100、PF ≥ 1.5、WF/MC 通過**
### Q6.3 Paper Trade 期間：**3 個月**

---

## 彙整需求摘要

| 維度 | 結論 |
|------|------|
| 標的 | BTC-USDT-SWAP、ETH-USDT-SWAP（永續） |
| 持倉頻率 | 彈性，幾分鐘 ~ 7 天皆可 |
| 執行方式 | 全自動 / 半自動皆可、規則化策略 |
| 資金 | < $1,000 USD |
| 最大回撤 | 10% |
| 槓桿上限 | 3x（建議起步 1-2x） |
| Alpha 來源 | 非價格指標優先（Tier 1：Funding、OI、F&G、訂單簿） |
| 策略型態 | 4 套並行：多指標一致性 / 逆勢 / 趨勢+確認 / 純 contrarian |
| ML | v1 不用，v2 視 v1 表現決定 |
| 回測門檻 | Sharpe ≥1.5、MaxDD ≤10%、Trades ≥100、PF ≥1.5、WF/MC 通過 |
| Paper 期間 | 3 個月 |
| 輸出 | TradingView Pine Script v6 → Bybit Python+ccxt bot |
| **LLM provider** | **OpenAI 直連**（gpt-5 + gpt-5-mini，非 Anthropic） |

---

## 最終流程指南：9 階段工作流

### 🌳 Git 工作流（強制標準，每次動工前先跑）

每次開新功能 / 改檔前：

```bash
# 1. 同步 upstream
git checkout main
git fetch upstream
git merge upstream/main
git push origin main

# 2. 切個人分支再動工（永遠不在 main commit）
git checkout -b feature/<topic>     # 或繼續用 my-research

# 3. 改檔 + 階段性 commit
git add <files>
git commit -m "..."
git push -u origin <branch>

# 4. 完工後可選：rebase + force-with-lease
git rebase main
git push --force-with-lease origin <branch>
```

**衝突風險**：
- 改 `research/`、`runs/`、`alpha-workflow.md` → 0%（新檔/新目錄，作者不會碰）
- 改 platform 內部（`agent/src/`、`agent/cli.py` 等）→ 高，要手動 resolve

---

### 🎯 階段 0：環境準備（一次性，約 1 小時）

**新手白話**：先把工具、帳號、資料源接好，後面才不會卡住。

**Model 配置（OpenAI 直連）**：
```
LANGCHAIN_PROVIDER=openai
LANGCHAIN_MODEL_NAME=gpt-5-mini   # 預設用 mini 跑量；策略生成階段改 gpt-5
OPENAI_API_KEY=sk-proj-...
OPENAI_BASE_URL=https://api.openai.com/v1
```

> ⚠️ Vibe-Trading 0.1.7 **不支援 Anthropic 直連**。Claude 路線須走 OpenRouter（加 ~5% 加價）。本流程採 OpenAI 直連。

**Checklist**：
- [ ] 安裝平台：`pip install vibe-trading-ai`，跑 `vibe-trading init` 引導建立 `.env`
- [ ] init 選 OpenAI provider → 貼 OPENAI_API_KEY → 預設 model 填 `gpt-5-mini`
- [ ] 申請 OpenAI key 前先去 Settings → Limits 設 monthly hard limit $20（防超扣）
- [ ] 確認 ccxt + OKX 連線：`vibe-trading run -p "Reply OK only"` 看是否成功
- [ ] Bybit testnet 帳號（後期才用）：https://testnet.bybit.com → API key 存 `.env`
- [ ] TradingView 帳號（Free 即可開始；要 webhook 自動下單需 Pro+）
- [ ] 建工作目錄：`mkdir -p runs research/lib research/strategies research/diagnoses`

> ⚠️ **永遠先用 testnet**。真錢進場前一切都該在沙盒驗證。

---

### 🔬 階段 1：因子初步分析（**純 local Python，無 LLM**）

**新手白話**：先把所有非價格指標都拉下來，看哪個對未來價格最有預測力 — 不要憑感覺，用統計說話。

**Model 配置**：**不用 LLM**。原因：mini 寫 API 整合 code 易死循環（v2 實證），且 IC/IR 計算純數值運算不需語言模型。

**資料源策略 — ccxt 是 2 年永續歷史的金鑰**

| 端點 | OKX 公開 | ccxt Binance | ccxt Bybit |
|------|---------|--------------|-----------|
| 永續 funding history | ~90 天 ❌ | **2 年** ✅ | **2 年** ✅ |
| 永續 OI history | 需 auth ❌ | 7-8 天 ❌ | 7-8 天 ❌ |
| 永續 OHLCV | ~60 天（淺端點） | 多年 ✅ | 多年 ✅ |
| 永續 mark/index price | 短 | 短 | 短 |

→ **強制要求**：所有歷史資料**走 ccxt（預設 Binance perp）**，OKX 只用即時 + 訂單簿。
→ 已實作 `research/lib/ccxt_data.py::fetch_funding_rate_history_ccxt`，驗證可拉 730 天 / 2190 rows。

**做法（純 Python）**：

1. 用 `research/lib/` 內的 fetcher 取資料：
   - `okx_data.fetch_funding_history`（短 ~90 天，僅備份對照）
   - `ccxt_data.fetch_funding_rate_history_ccxt`（**主用**，Binance 2 年）
   - `ccxt_data.fetch_ohlcv_ccxt`（多年 OHLCV）
   - `sentiment.fetch_fear_greed`（alternative.me，2 年無壓力）
   - `ccxt_data.fetch_oi_history_bybit`（**注意**：公開只回 7-8 天）

2. 用 `lib/factor_metrics.py` 算：
   - `add_forward_returns()` → 多 horizon forward returns
   - `evaluate_factor()` → IC（Spearman）+ IR（rolling 30d）+ sample size

3. 用 `lib/report.py` 產 markdown 報告。

**範例 orchestrator**：見 `research/factor_extended.py`（已實作，2 年 BTC + 3 因子）

**白話術語**：
- **IC（Information Coefficient）**：因子值與未來報酬的相關係數。\|IC\| > 0.05 算有意義、> 0.1 算很強。
- **IR（Information Ratio）**：IC 的「穩定性」。IR > 0.5 算好因子（不是偶然贏的）。

**通過條件**：至少 2 個因子 \|IC\| > 0.05 且 \|IR\| > 0.5。若不通過 → 擴大 Tier 2 因子重試。

**v3 強制規則**：

1. **單一因子 \|IC\| < 0.10 不可單獨建策略** — 弱因子必組 ensemble（多因子投票稀釋雜訊）。單因子 \|IC\| ~0.08 的策略在敵對 regime 會被結構性逆風吃光，須與其他因子組合。

2. **Cross-regime 強制驗證（≥ 3 期）** — IC 計算不能只看 2 年單區間。任何因子分析報告須包含至少：
   - 1 個 bear cycle（如 2022 LUNA/FTX、2018 緩跌）
   - 1 個 bull cycle（如 2024-26、2020-21 alt-bull）
   - 1 個 chop / 整理期（如 2023 post-FTX）
   
   各期 IC 列表並標注「regime-conditional」vs「regime-stable」屬性。Regime-stable 因子優先進策略。

---

### 🧪 階段 2：策略原型產生（**gpt-5 主跑**）

**新手白話**：基於上一步「哪些因子有用」的結論，由 gpt-5 設計 4 種風格的交易規則。

**Swarm**：**`crypto_trading_desk`** ⭐（含 funding_basis_analyst + liquidation/microstructure + on-chain/flow + risk_manager — 為永續合約 + 非價格 alpha 量身打造）

> ⚠️ `derivatives_strategy_desk` 看名稱像對的，**實為 options/Greeks**。永續一定要用 `crypto_trading_desk`。

**Model 切換**：先改 `.env`：
```
LANGCHAIN_MODEL_NAME=gpt-5
```

**指令**：
```bash
vibe-trading --swarm-run crypto_trading_desk \
  '{"target":"BTC-USDT-SWAP","timeframe":"1d ~ 7d","upstream_context":"見 factor_importance_report.md"}'
```

或直接 prompt（避免迭代 loop，禁止 agent 寫 Python）：
```
基於 factor_importance_report.md，分別產出 4 套永續合約規則策略：
  S1. 多指標一致性：funding/F&G 同方向才開倉
  S2. 逆勢均值回歸：funding 極端時反向
  S3. 趨勢+指標確認：價格 EMA 趨勢主導，funding 確認
  S4. 純 contrarian：funding 與 F&G 雙重極端反向
每套含進場/出場/停損/倉位大小規則。
持倉時長：1h ~ 7d 皆可，自選最適頻率。
限制：槓桿 ≤ 3x、單筆風險 ≤ 帳戶 2%、單一標的（BTC 或 ETH）。
策略思想要求：percentile-based 閾值（非絕對值）、persistence 2/3 settlements、ATR-based stops。
DO NOT write Python. ONLY use write_file tool.
輸出：research/strategies/strategy_S1.yaml ~ S4.yaml。
```

**產出**：4 份 `strategy_Sn.yaml` 策略規格（含參數搜尋範圍）

> 注意 sandbox：write_file 落腳 `runs/<run_id>/research/strategies/`，需 cp 到 repo 真實位置。

---

### 🛂 階段 2.5：Regime detection 層（v3 新增）

**新手白話**：策略不能不分牛熊都用同一套 — 同一個 contrarian 策略在熊市可能 Sharpe +2，在牛市 -0.7。要先有「現在是哪種市場」的偵測器，才能決定該不該開倉。

**為何必須**：

任何 regime-conditional 策略（特別是 contrarian、mean reversion、funding 反向類）需要這層 gate。實證：
- 同一套 funding 反向策略在 bear regime Sharpe > +0.8，bull regime Sharpe < -0.5
- 拿掉 gate 在強牛 OOS（如 covid 後 alt-bull）DD 可達 -38%
- 加 gate 後 OOS DD < -2%

**設計範本（不寫具體數字，視策略客製）**：

1. **價格 trend 元件**：移動平均（EMA / SMA）+ slope 過濾
   - 例：N 日 EMA vs 現價、N 日 slope 方向
2. **持續性過濾**：避免單日反彈被誤標
   - 例：滾動 M 天內 X% 比例為 raw bear，才確認 bear
3. **因子方向 override**：用主要因子的均值符號當額外確認
   - 例：funding 連續正值（市場狂熱）即使技術面看似 bear 也降級 neutral
4. **三類標籤**：bull / bear / neutral
5. **Gate 規則**：策略只在指定 regime 內開倉，其餘強制歸零

**參數選擇方法**：

- 不靠人類直覺定數字，跑 grid search 找 Pareto 最佳
- 評分指標：跨 N regime 的 Sharpe sum（不是單 regime 最佳）
- 警惕「緊縮 detector」過敏（如 30d/83% 持續門檻會吃 bumpy bear 的 dead-cat 反彈）

**驗證方法**：

1. 計算各歷史 regime 的 label distribution：
   - 已知 bear 期間：bear% 應 ≥ 70%
   - 已知 bull 期間：bull% 應為主，bear% 用來捕捉回檔（10-35% 合理）
   - 已知 chop 期間：neutral % 高
2. 跑 gate 開關對比實驗：策略在「無 gate」vs「有 gate」的 OOS DD 與 Sharpe 差異
3. 至少 1 個 OOS regime（從未調 detector 參數的時段）驗 label 行為合理

**實作參考**：`research/lib/regime.py`（generic detector）+ `research/regime_validate.py`（label 分佈驗證範本）

**通過條件**：detector 在 OOS regime 上 label 分佈與該 regime 真實特性一致，且加 gate 後策略 OOS DD ≤ ungated DD / 3。

---

### 📊 階段 3：統一回測 + 統計驗證 + 診斷反饋

**新手白話**：把 4 套策略丟到同一個歷史時段「跑過去」，看誰賺最多 / 跌最少 / 最穩。**每個策略回測完強制走「diagnose 反饋迴圈」**。

**Model 配置**：
- 回測本體：**不用 LLM**，純 `python -m backtest.runner <run_dir>`
- 診斷階段：**gpt-5**（須判斷力）

**標準流程（每個策略各跑一遍）**：

```
1. 用 yaml → 寫 signal_engine.py（人工或 gpt-5 輔助）
2. 寫 config.json（src=ccxt、interval=1H、initial_cash=1000、leverage、fees）
3. 跑 `python -m backtest.runner runs/<strategy_id>`
4. 讀 artifacts/metrics.csv + trades.csv
5. 餵給 vibe-trading backtest-diagnose（gpt-5）→ 得結構化診斷
6. Claude 對話 review 診斷 → 決定改哪、回到 Phase 2 還是 4
```

**為何「diagnose 反饋」不能省**：
- 自己讀 metrics 容易漏（如 engine/interval mismatch、FNG lag、ATR sizing 等）
- gpt-5 結構化清單比人類嚴謹（v2 walk-through 實證一次抓出 4 件人類漏掉的）
- diagnose 建議**仍要回測驗證**（v2 機械套用導致 DD 從 15% → 42% 真實翻車）

**配置 baseline（apples-to-apples 比較）**：
- 標的：BTC-USDT（spot proxy，因 ccxt loader 不收 -SWAP 後綴）
- 期間：2024-05-14 ~ 2026-05-13（2 年 + ccxt funding 上限）
- 來源：`source: "ccxt"`
- 間隔：`interval: "1H"`
- 引擎：`engine: "daily"`（CryptoEngine 內含 funding fee + 24/7）
- 起始：$1000
- 手續費：taker=0.00055、maker=0.0002、slippage=0.0005
- Walk-Forward：6 個月窗
- Monte Carlo：1000 次 bootstrap

**通過門檻**（任一未過 → 該策略 fail，回 Phase 4 優化或 Phase 2 重設計）：
- Sharpe ≥ 1.5
- Max Drawdown ≤ 10%
- 成交數 ≥ 100
- Profit Factor ≥ 1.5
- Walk-Forward 各窗 Sharpe ≥ 1.0（穩定性）
- Monte Carlo 95% CI 報酬不跨 0（不靠運氣）

**白話術語**：
- **Walk-Forward**：把歷史切成多個窗，每個窗「先學前半 / 測後半」，模擬未來表現。
- **Monte Carlo**：把交易順序隨機重排 1000 次，看績效分布是否穩。

**產出**：每策略一份 `runs/<strategy_id>/artifacts/`（metrics.csv、trades.csv、equity.csv）+ `research/diagnoses/<strategy_id>_diagnosis.md`

#### ⚠️ 回測沙箱踩雷清單（**首次跑必看**）

按發生順序：

1. **目錄名衝 Windows 保留字**：`aux/` 是 DOS device 名 → 用 `factor_data/`
2. **AST 白名單拒負數常數**：`FUNDING_LOW = -0.00005` 在 class body 被拒（UnaryOp 非 literal）→ **負數常數必須移 `__init__`**
3. **`sig.values[mask] = x` read-only error**：用 `sig.iloc[mask] = x`
4. **時區感知 parquet 寫不進**：存前 `df.index = df.index.tz_convert(None)`
5. **F&G 有 string 欄位 (classification)**：parquet 不愛 object dtype，存前 `df = df[['fng']]` 只留數值
6. **config 鍵名**：`initial_cash` 不是 `initial_capital`（看 base engine `config.get`）
7. **OKX `/market/candles` 端點淺**（~1440 bars） vs `/market/history-candles` 深（多年）；platform OKX loader 用淺端點 → 改用 `source: "ccxt"`
8. **ccxt loader 符號轉換**：`code.replace("-", "/")` → `BTC-USDT-SWAP` 變 `BTC/USDT/SWAP`（非法）。改用 `BTC-USDT`（spot proxy）作 walk-through
9. **平台 read_file 沙箱**：agent 讀不到 repo 絕對路徑 → 要透過 prompt inline 或 `VIBE_TRADING_ALLOWED_FILE_ROOTS` env

**v3 強制三檢查**（任何策略進階段 4 前必跑，缺一不可）：

1. **Cost stress test（成本壓力測試）**
   - 把 config 的 maker_rate / taker_rate / slippage 全部 × 3 重跑
   - 若 base Sharpe > 1.0 但 stress Sharpe < 0 → alpha 是 fee illusion，禁進實盤
   - 通過條件：stress Sharpe ≥ base Sharpe × 0.5 且仍為正
   - 揭示「signal 結構穩 vs alpha 被費吃」的差別：DD 變化 < 5pp 是好，return 被吃 50%+ 通常是 frequency 太高

2. **Parameter sweep（參數掃描）**
   - 對策略主要參數（持續性、percentile window、EMA 長度等）做 grid search（≥ 5 個鄰近 config）
   - 評分用「跨 N regime 的 Sharpe sum」，不是單期最佳
   - 若 metric 隨鄰近參數劇烈震盪 → cherry-pick / overfit
   - 若 metric 形成 plateau → 真實 alpha，選 plateau 中心當 production 參數
   - 警惕只比較單一 regime 結果決定參數，這會 overfit 該 regime

3. **OOS walk-forward（樣本外驗證）**
   - 至少 2 個從未拿來調參的 regime（且 character 需多樣 — 不能 2 個都是 bull）
   - 例：訓練用 bear2022 + bear2018 + bull2024-26 → OOS 用 covid_altbull + post-FTX_chop
   - 通過條件：OOS DD ≤ 訓練期 max DD × 1.5、OOS Sharpe ≥ 0
   - 任一 OOS 翻車（DD > -25% 或 Sharpe < -0.5）→ 退回階段 2 重設計，不准進實盤

三項全過 → 進階段 4 微調。任一不過 → 退階段 2。

---

### 🔧 階段 4：保守參數優化（**gpt-5 主判斷**）

**新手白話**：在通過門檻的策略中找最佳參數，但**不追求峰值**，要找「鄰近參數也同樣好」的穩定點（避免過擬合）。

**Model 配置**：`.env` 保持 gpt-5（接續 Phase 2 / Phase 3 diagnose）

**Swarm**：`quant_strategy_desk`

**做法**（無內建 grid search CLI，DIY 迴圈）：
```python
import itertools
windows = [30, 60, 90, 120]
thresholds = [0.7, 0.75, 0.8, 0.85, 0.9]
for w, t in itertools.product(windows, thresholds):
    rewrite_signal_engine(w, t)   # 改常數
    metrics = run_backtest()
    record(w, t, metrics)
# 選「鄰域 ±20% 平均 Sharpe > 峰值 80%」的參數組
```

**重點原則**：
- **穩定性 > 峰值績效**：選鄰域 robust 的、不選單點最高
- **Out-of-sample 留白**：留最後 20% 資料完全不調參作 holdout
- **Walk-Forward CV**：滾動視窗 train/test，跨窗一致才算真

---

### 🏆 階段 5：策略選擇

把通過 Phase 3 + 4 的策略並列：
| 策略 | Sharpe | MaxDD | PF | WF 穩定度 | 入選 |
|------|--------|-------|----|--------|------|
| S1 | 1.8 | 8% | 1.7 | ✅ | ✅ |
| S2 | 1.3 | 12% | 1.4 | ❌ | ❌ |
| ... |

**選擇**：通過全部門檻 + Sharpe top 1 → 主策略；top 2 → 備用。

**若全部 fail**：
- 退 Phase 1 加 Tier 2 因子（穩定幣流、大戶多空比）
- 或放寬持倉時長限制
- 或考慮 regime-aware multi-strategy（不再追求單一全天候策略）

**v3 評分原則（取代「Sharpe top 1」單一選法）**：

1. **Ensemble > 單套**：若單策略 IC 弱（|IC| < 0.10）或跨 regime 性格差異大（同策略在 bear2022 vs bear2018 結果反轉），組 ensemble 多因子投票 + 倉位自動降權（單票觸發 << 多票同向）。Diversification 是補弱因子最直接方法。

2. **Pareto 跨 regime sum > 單期峰值**：選策略時用「N 個 regime 的 Sharpe 加總」排名，不只看單 regime 最高。單期 Sharpe +2.5 但其他 regime -0.5 的策略，比四個 regime 都 +0.8 的策略更脆。實證：peak-chasing 在 OOS 常翻車，sum-based 通常穩。

3. **Asymmetric variant 三版必跑**：long-only、short-only、long+short 全部回測同一份訊號。若信號 IC 在「短側」遠強於「長側」（如 funding 反向 contrarian），short-only 通常 Sharpe/Calmar 顯著優於 long+short。不要假設對稱。

4. **Regime gate 是 production 必要安全閥**：即使單套策略看似全 regime 都正，OOS 環境會出現訓練時沒見過的 regime 性格（如 alt-bull / chop）。Gate 過濾掉非適配 regime 是 OOS 翻車防火牆。實證：拿掉 gate 在 OOS bull regime DD 可達 -38%；加 gate 後 -2%。

5. **Cost-adjusted 最終排名**：Sharpe top 不是 base Sharpe top，而是 stress Sharpe top（3× cost 後）。實戰執行費用 + 滑價會不只 3×，stress 通過的策略才有實戰價值。

---

### 📜 階段 6：TradingView Pine Script 導出

**新手白話**：把選中的策略寫成 TradingView 可貼上去跑的腳本，視覺驗證一次。

**Model 配置**：不用 LLM（CLI 一次性命令）

**指令**：
```bash
vibe-trading export \
  --strategy strategy_S1_optimized.yaml \
  --format pine-v6 \
  --output S1.pine
```

或從 swarm run 直接導：
```bash
vibe-trading --pine <run_id>
```

→ 把 `S1.pine` 內容貼到 TradingView Pine Editor → Add to chart → 跑 Strategy Tester。

**驗證**：TradingView 回測淨值曲線形狀應與平台回測 ±5% 內吻合。

**Alert 設定**：Strategy 內加 `strategy.entry/exit` → 在 TradingView Create Alert → 設 webhook URL（需 TradingView Pro+）。

**v3 限制盤點 — TradingView 不能取代 Python 執行**：

實作後發現 TV Pine 對策略類因子的支援嚴重不足：

1. **無乾淨 funding rate time-series feed**：TV 沒標準的「`<EXCHANGE>:BTCUSDT_FUNDING_RATE`」ticker，多數人會誤拿 perp 收盤價當代理，結果 funding 百分位變成「價格百分位」，訊號完全錯誤
2. **無 alternative.me F&G feed**：所有 sentiment 因子要 fallback 到 BTC.D / USDT.D 等 proxy，與原訊號 drift 大
3. **無 OI / 鏈上資料**：Tier 1 非價格因子大半在 TV 上無原生來源
4. **無多源 funding 合併**：Python 可串 BitMEX（2017+）+ Binance（2019+）拉長歷史；TV 單一 ticker 受限
5. **不能下 crypto 單**：TV strategy 只能跑回測 + alert webhook，真實執行得自架 server 接 webhook → ccxt 下單

**TV 定位調整**：
- ✅ **regime 監控儀表板**（看 EMA + slope 標 regime 背景色，用來決策進出場時機）
- ✅ **價格 / 趨勢類因子視覺化**（EMA crossover、ATR、RSI 等 TV 強項）
- ❌ **不當主執行平台**（funding / FNG / OI 為主的策略訊號精度差）
- ❌ **不當主回測平台**（Strategy Tester 與 Python backtest 落差大）

**真實執行路徑**：Bybit Python bot（ccxt）+ 自寫 live loop（fetch → signal → order → log）。TV 只負責看圖確認 regime。

---

### 🧫 階段 7：Paper Trade（3 個月）

**新手白話**：用假錢實際跑，盯實際成交與回測結果差多少。

**Model 配置**：不用 LLM

**選項 A**：平台 paper engine（簡單，但非真實掛單）
**選項 B**：Bybit testnet（真實撮合，**推薦**）

**Bybit testnet 設定**：
```bash
vibe-trading paper-trade \
  --strategy strategy_S1_optimized.yaml \
  --exchange bybit \
  --testnet \
  --capital 1000 \
  --duration 90d \
  --review-interval 7d
```

**每週 review 重點**：
- 實盤 Sharpe vs 回測 Sharpe（若 live < 50% backtest → 警訊）
- 實際滑點與假設滑點差距
- 是否有「回測有訊號、實盤掛單未成交」（流動性問題）

**自動暫停線**：累計 DD 達 5% → 暫停執行人工 review；達 7% → 終止重檢。

**3 個月通過條件**：
- live Sharpe ≥ backtest Sharpe × 0.7
- MaxDD ≤ 8%（比回測門檻嚴）
- 無 critical bug

---

### 🚀 階段 8：Bybit 實盤遷移（用 ccxt）

**新手白話**：把 Pine 邏輯重寫成 Python，掛 Bybit 真盤。

**Model 配置**：不用 LLM（或用 gpt-5 輔助轉檔 review）

**轉換工具**：
```bash
vibe-trading export \
  --strategy strategy_S1_optimized.yaml \
  --format ccxt-bot \
  --exchange bybit \
  --output bot_S1.py
```

> 注意：Pine → Python 不是逐行翻譯，是「邏輯重寫」。平台自動處理大部分但需人工 review。

**Bybit testnet 再跑 2 週**驗證 ccxt 包出來的 bot 邏輯與 Pine 一致。

**真盤啟動三段式**：
| 階段 | 倉位比例 | 條件 |
|------|---------|------|
| Step 1 | 回測倉位的 10% | 起步，30 天無重大偏差 |
| Step 2 | 30% | Step 1 通過後 |
| Step 3 | 50%（最高） | Step 2 通過 60 天 |

> **永遠不開到 100% 回測倉位**。真實市況一定比回測殘酷，預留 50% 緩衝。

**Kill Switch**（必設）：DD 達 7% bot 自動停 + 推送 Telegram alert。

---

### 📡 階段 9：監控 & 退役

**Model 配置**：不用 LLM（定期人工 review）

**每週**：對照 live vs backtest 績效
**每月**：策略生命週期評估
**退役觸發**：
- live Sharpe 連續 2 個月 < backtest × 0.5
- DD 達 10%
- 因子失效（IC 跌至原本 < 30%）→ 退 Phase 1 重做

---

## 工具 / Swarm / Skill / Model 對照表

| 階段 | Swarm Preset | 主要 Skills | Model |
|------|-------------|------------|-------|
| 1 因子分析 | **n/a**（純 local Python） | research/lib/* (okx_data, ccxt_data, sentiment, factor_metrics) | **無 LLM** |
| 2 策略產生 | **`crypto_trading_desk`** ⭐ | strategy-generate、perp-funding-basis、crypto-derivatives、execution-model、risk-analysis | **gpt-5** |
| 3 回測（執行） | **n/a**（`python -m backtest.runner`） | backtest-diagnose、quant-statistics | **無 LLM** |
| 3 回測（診斷） | **n/a**（單獨 `vibe-trading run`） | backtest-diagnose | **gpt-5** |
| 4 優化 | `quant_strategy_desk` | backtest-diagnose、quant-statistics、risk-analysis | **gpt-5** |
| 6 Pine 導出 | n/a (`--pine RUN_ID`) | pine-script | n/a |
| 8 ccxt bot | n/a | ccxt、execution-model、vnpy-export | n/a（或 gpt-5 review） |

**Model 分工原則**：
- **gpt-5-mini**：資料 fetch、批次小判斷（量大、便宜、快）— Phase 0 預設值
- **gpt-5**：策略設計、診斷解讀、參數判斷、最終 go/no-go（需推理力）— Phase 2/3-diagnose/4

---

## 用戶疑問回答（最終）

### Q：策略能否輸出為 TradingView 可用的腳本？
**A**：✅ 可以。Phase 6 用 `vibe-trading export --format pine-v6` 直接出 Pine Script v6。免費 TradingView 帳號就能跑回測；要 webhook 自動下單需 Pro+ ($14.95/月)。

### Q：之後改 Bybit 可量化交易腳本是否可行？
**A**：✅ 可行，**但不是直譯**。Pine Script 是 TradingView 內專屬語言，無法在 Bybit 跑單。Phase 8 用 `vibe-trading export --format ccxt-bot --exchange bybit` 將同樣策略邏輯重寫為 Python + ccxt，直接連 Bybit REST/WebSocket。Pine 階段是「視覺驗證」，ccxt 階段是「實際執行」。轉換時平台處理大部分，但**需人工 review 至少一次**確認邏輯一致。

---

## 驗證方法（端到端驗收）

判斷整套流程跑通的測試：

1. **Phase 1 通過**：產出 `factor_importance_report.md`，至少 2 個因子 \|IC\| > 0.05 且 \|IR\| > 0.5
2. **Phase 2 通過**：產出 4 份 `strategy_Sn.yaml`，每份含完整 entry/exit/stop/size 規則
3. **Phase 3 通過**：產出 `backtest_comparison_dashboard.html`，至少 1 套策略過全部門檻
4. **Phase 3 diagnose 反饋**：每套策略產出 `research/diagnoses/<id>_diagnosis.md`
5. **Phase 6 視覺驗證**：TradingView Pine 回測淨值曲線與平台回測 ±5% 內吻合
6. **Phase 7 進入紙交**：Bybit testnet 第一週至少 5 筆成交且滑點 < 0.1%
7. **三個月後 paper review**：live Sharpe ≥ backtest Sharpe × 0.7 → 進實盤
8. **實盤第一個月**：DD < 5%、無 kill switch 觸發 → 可加倉

---

## 關鍵風險 & 新手陷阱提醒

1. **過擬合（Overfitting）**：參數調太細 → 回測漂亮、實盤垃圾。對策：Walk-Forward + 鄰域穩定性檢驗。
2. **小資金摩擦成本**：$1k + 0.055% taker fee，單筆交易 ≥ $50 才划算。低頻策略優先。
3. **funding rate 反向風險**：funding 多頭擁擠不代表立刻反轉，可能持續數週。設停損。
4. **資料倖存偏差**：用「現在還活著的 token」訓練 → 高估表現。BTC/ETH 較不受此影響。
5. **手續費忘了算**：回測必加 taker 0.055% 雙邊 + 滑點 0.05%。
6. **3x 槓桿心理門檻**：永續即使 3x 也可能在劇烈波動中被穿倉。建議起步 1x。
7. **API rate limit**：跑因子分析時需加 rate limiter（OKX/ccxt 都有）。
8. **regime 依賴**：所有策略都有「擅長 regime」，無真正全天候單策略。要在多 regime 樣本驗證真假 alpha（如 BTC 2022 bear + 2024 bull）。

---

## 第一步落地（建議今日就做的）

```bash
# 1. 安裝（若尚未）
pip install vibe-trading-ai
cd C:\Users\cool6\Vibe-Trading
vibe-trading init  # 引導建立 .env

# 2. init 時答：
#    Provider: OpenAI
#    OPENAI_API_KEY: sk-proj-...
#    Default model: gpt-5-mini
#    Tushare token: (skip)

# 3. 列出 swarm preset 確認名稱與 template vars
vibe-trading --swarm-presets
vibe-trading --swarm-inspect crypto_trading_desk

# 4. Phase 1（純 Python，無 LLM）：
python research/factor_extended.py    # 拉 2 年 funding+F&G+OHLCV，算 IC/IR

# 5. 看 factor_extended.md 結果。若至少 2 因子 |IC|>0.05 → 進 Phase 2
```

---

## 已知不確定 / 需執行時驗證

1. **swarm preset 接受的 template vars**：每個 preset YAML 內 `{var}` 名稱不同。執行前用 `--swarm-inspect <preset>` 確認。
2. **Per-agent model 混合**：CLI 沒有直接 flag。要 mini/gpt-5 同一 swarm 內混跑，需 fork preset YAML 在各 agent 加 `model:` 欄位（進階）。**簡化作法**：每階段切 `.env` 重跑（本流程採用此法）。
3. **Pine Script 導出格式**：`--pine RUN_ID` 是 show，要寫檔可能另需 `--code RUN_ID` 或 export 工具。需試跑確認。
4. **ccxt bot 導出**：未確認有 `--format ccxt-bot` 直接導出。可能需透過 `vnpy-export` skill 或自行包裝。

---

## 附錄 A：v1/v2 walk-through 真實結果（baseline 參考）

| 條件 | Sharpe | MaxDD | PF | Win rate | Trades | Excess vs BTC |
|------|--------|-------|-----|---------|--------|--------|
| S1 v1 (90d, $1k) | 2.65 | 4% | 4.46 | 88% | 8 | -7% |
| S1 v1 (2y, $1k) | 0.12 | 16% | 1.10 | 59% | 34 | -26% |
| S1 v2 (+ATR+regime) | -0.63 | 20% | 0.70 | 33% | 30 | -39% |
| S2 baseline (2y) | -0.49 | 71% | 0.86 | 49% | 160 | -73% |
| S3 baseline (2y) | -0.28 | 34% | 0.95 | 40% | 247 | -50% |
| S4 baseline (2y) | -0.73 | 54% | 0.64 | 49% | 82 | -64% |

→ **單純套 best-practice 不一定更好**。所有改進需回測驗證才採用。
→ **所有 4 套在 BTC 2024-2026 bull cycle 都跑輸 HODL**，凸顯 contrarian 在強趨勢中系統性逆風的問題。

---

## 附錄 B：強制 baseline policy

- 任何新策略**先跑 v1 baseline（簡單規則 + 無 exit）**作對照
- v2/v3 改進**必須打敗 v1 baseline**（Sharpe、DD 雙贏）才採用
- 改進不過 baseline → **退回去掉該變更**
- 寫死於 Phase 3 流程：每次優化後對照 v1，**未過退回**

---

## 附錄 C：未來考慮

1. **regime detection 多策略組合**：bull / bear / range 各跑專屬策略 → 動態權重，比單一全天候現實
2. **跑 2022 bear cycle**：用同 4 套策略測 2022-01 ~ 2024-01 兩年 bear，驗證 contrarian 條件性 alpha
3. **freqtrade 升級路線**：Phase 8 實盤可考慮遷移到 freqtrade（成熟框架、Telegram alert、hyperopt 內建）
4. **加 Tier 2 因子**：OI history 改付費 Coinglass、加 basis term structure、加 stablecoin flow（DeFiLlama）
