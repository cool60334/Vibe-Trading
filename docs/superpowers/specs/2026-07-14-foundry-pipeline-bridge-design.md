# Foundry → Pipeline Bridge — Design

> Date: 2026-07-14 · Branch: `quant-trading-dashboard`
> Status: design approved, pending implementation plan
> Reviewers: self + agy adversarial review ×3（設計 2 輪 + spec 文件 1 輪）。
> 每點皆對真實原始碼裁決：採納者標明，駁回者附碼證（如 agy 誤判「未對庫內去重」——實際 gate 已 reject `abs_spearman>=0.7`）。

## 1. Problem

Talos **Foundry**（`research/hermes/`）是自主因子鍛造引擎：LLM 寫因子碼 → docker 沙盒 → gatekeeper（`gross_ic`/`DSR`/`regime_ic`/OOS-lock/`nearest_correlate` 去重）。產出落在 `research/manifests/candidate_features/cand_<sym>.parquet`（因子值）+ `foundry_evidence/foundry_evidence_<sym>.json`（evidence cards）。

**Foundry 目前是孤島**：grep 全 repo，沒有任何下游讀 `candidate_features/`。9 階段 pipeline（`research/pipeline/`）用**自己的** stage0 discovery（從 stage0a evidence 按 IC 挑既有因子），跟 Foundry 各走各的。Foundry 嚴選出的因子**永遠不會變成策略**。

本 spec 補這座橋：讓 Foundry 嚴選的因子能被 pipeline 的 stage2→5 拿去**建策略 / 回測 / OOS 選拔**。

## 2. Locked decisions（brainstorm 定案）

| 決策 | 值 | 理由 |
|---|---|---|
| **範圍** | 只做橋，不含排程（自動觸發另一份 spec） | 單一 spec 聚焦、好驗 |
| **Authority** | 信任 Foundry 閘，直達 stage2，**跳過 stage0/1 重篩** | Foundry gate 比 stage1 IC-0.03 嚴（含 DSR/PBO/OOS-lock）；重篩=用較寬的閘重判，丟掉嚴格度 |
| **Production 安全** | 發掘自動、**部署人工閘** | `features_<sym>.parquet` 是 live trader 讀的 production 檔，`promote.py` 是唯一寫它的路（人工 `--confirm`，明文禁止自動鏈碰）。橋只餵研究路 |
| **值曝露** | per-run 暫存快取 + 單一 loader 讀取 | 不碰 production；跨 stage subprocess 可傳；每 run 重生免漂移 |
| **重算** | 沙盒重跑因子碼算全 span 值 | Foundry 存的值只到 pre-oos，OOS 回測需要全 span |

## 3. Two blocking findings（設計前置，讀碼驗出）

### 3.1 Foundry 存的值只到 pre-oos
`cand/graveyard_<sym>.parquet` 索引止於 **2024-12-31**（`oos_start=2025-01-01` 前），`features_<sym>.parquet` 到 2026-06。Foundry `foundry_split` 故意保留 OOS 窗沒算。

→ **合併 Foundry 凍結值 = OOS 窗全 NaN** → stage3 OOS 回測 / stage5 OOS 選拔拿不到訊號 → 策略無法 OOS 驗證 → 死。
→ Foundry 因子的單位是**程式碼（公式）**，honest OOS 回測**必須把碼在全 span 因果重算**，不能重用凍結值。

### 3.2 Foundry 不存 forge 碼原文（§A 前置需求）
Evidence card 只有 `code_sha256`（雜湊）+ `formula`（zoo LaTeX 描述，非可跑 Python）。`candidate_store` 只存值。forge LLM 寫的可跑碼**跑完就丟**。

→ 「沙盒重跑因子碼」現在**做不到**。前置需求：**Foundry 先把 forge 碼存下來**。

## 4. Architecture

```
Foundry run
  → cand_<sym>.parquet (pre-oos 值, 對帳/去重用)
  → evidence cards
  → §A code store: forge 碼 + code_sha256 + base image id   ← 新增(前置)

Bridge step (研究路, 每 pipeline run 跑一次)
  → 讀 cards 篩 verdict=candidate
  → 每因子: 讀碼 → 校 sha → docker 沙盒對全 span panel 重算 → 全 span 因果值
  → pre-oos 切片 vs cand 存值 對帳, 不符跳過
  → 卡片 → FactorEntry (含 classify_stability(regime_ic))
  → 落 per-run 暫存: runs/<job>/foundry_overlay_<sym>.parquet
                     runs/<job>/foundry_manifest_<sym>.json

單一 loader (讀取層, 絕不執行運算)
  → load_features(sym, include_foundry) : production ∪ overlay 快取 (記憶體 concat)
  → load_manifest(sym, include_foundry) : stage1 manifest ∪ foundry entries
  → flag 預設關 → trader/production 路完全不受影響

stage2b / stage3 / stage5 (研究, include_foundry=True)
  → 建策略 → 回測(oos_start 分割) → OOS 選拔
  → 選出策略 → 人工 promote.py → production/trader
```

## 5. Components

### §A Foundry code persistence（前置，foundry 側）
- `forge` / `process_hypothesis` 對 `verdict=candidate` 的因子，把 `fr.code` 寫進 code store：
  `candidate_features/code/<sym>/<factor_id>.py`
- 同址存 metadata：`code_sha256`（完整性）、`base_image_id`（沙盒 image content-addressed id，依賴漂移偵測）、`interval`、`horizon_h`。
- 只存 candidate（graveyard 不需重算）。

### §B Bridge module（`research/hermes/foundry_bridge.py`）
每 symbol：
1. 讀 evidence cards，篩 `verdict == candidate`。
2. 每因子：
   a. 讀 §A code store 的碼，校 `code_sha256`（不符 → 跳過記 log）。
   b. docker 沙盒對**全 span** panel（`features ∪ ohlcv`）重算 → 全 span 因果值。
   c. **對帳**：重算的 `[:oos_start]` 切片 vs `cand_<sym>.parquet` 的存值，
      **`np.allclose(..., equal_nan=True)`** 不符 → 跳過記 log（抓非因果殘留 + 環境/image 漂移）。
      `equal_nan=True` 是**必要**的：因子 rolling burn-in 期本來就有 NaN，預設 `equal_nan=False` 會把
      行為完全一致的合法因子誤殺。（repo 先例：forge 自己的 `pit_check_via_sandbox` 就用 `equal_nan=True`。）
3. 卡片 → `FactorEntry`：
   - `name` ← `foundry_` 前綴 + `factor_id`（撞名防護）
   - `ic_by_horizon` ← `{horizon_h: gross_ic}`（單鍵；`archetype_router` 是 `for h,v in ic_by_horizon.items()` 取 max-abs，優雅降級，已驗不炸）
   - `ir` ← `ir`；`sample_size` ← `n_samples`
   - `cross_regime_ic` ← `regime_ic`（Foundry pre-oos 算）
   - `stability` ← **複用 `research.factor_regime.classify_stability(regime_ic)`**（回 `regime_stable`/`conditional`，語意 100% 對齊 stage1，非自創 autocorr，且來源是 pre-oos regime_ic 無 OOS 洩漏）
   - `verdict` ← 非-`reject`（`single_use`，若 `classify_stability` 判 `conditional` 則依 `refine_verdict` 降 `ensemble_only`）
4. 防禦：
   - 撞名檢查——`foundry_<id>` 若仍撞 production feature 名 → 跳過記 log。
   - `overlay_df = overlay_df.reindex_like(prod_features)`；`assert len(overlay_df) == len(prod_features)`（index 對齊災難防護）。
5. **Candidate 數量上限（OOM 防護）**：overlay 的因子數設 cap（config 可調，預設如 50），
   超過則按 Foundry 卡片的 `dsr` / `|gross_ic|` 取 top-K。Foundry 日積月累可能吐出上千 candidate，
   全 span 展開一次性 concat 進 subprocess 會 OOM 拖垮整條 pipeline。

6. 落 **per-run 暫存快取**（不落持久 production 路）：
   `runs/<job>/foundry_overlay_<sym>.parquet`、`runs/<job>/foundry_manifest_<sym>.json`。
   - **生命週期**：快取屬於該次 pipeline run。job 結束時由 pipeline manager teardown 刪除該 job 的
     overlay 檔（或沿用 `dashboard/server/retention.py` 既有保留策略）。不清理會累積孤兒 parquet 塞爆磁碟。

### §C 單一 loader（`research/lib/factor_io.py` 擴充）
- `load_features(sym, manifests_dir=None, include_foundry=False)`：
  - 預設只讀 `features_<sym>.parquet`。
  - `include_foundry=True` → 讀 production + per-run overlay 快取，記憶體 `pd.concat(axis=1)`。**loader 絕不呼叫沙盒 / 絕不執行運算。**
- `load_manifest(sym, ..., include_foundry=False)`：預設讀 `factor_<sym>.json`；flag 開則 append per-run `foundry_manifest_<sym>.json` 的 entries。
- 把 `stage2b_compile_signal` / `stage3_backtest` 散落的 `read_parquet(features_<sym>)` 收斂成呼叫 `load_features`（單一讀入口；未來接第 3 套系統只改這裡）。
- **跨 subprocess 傳遞**：pipeline 各 stage 是獨立 python subprocess，記憶體不共享，故 flag + 快取位置**皆走 env**：
  - `RESEARCH_INCLUDE_FOUNDRY=1` 開啟 overlay。
  - `RESEARCH_FOUNDRY_OVERLAY_DIR=<per-run cache dir>` 指向 §B 落的 `foundry_overlay_<sym>.parquet` / `foundry_manifest_<sym>.json`。
  - 兩者由跑 pipeline 的一方（bridge step 或呼叫者）設定；預設**皆未設** → trader / production 路零影響、loader 行為與現況完全一致。
  - **嚴格字串比對**：loader 用 `os.getenv("RESEARCH_INCLUDE_FOUNDRY") == "1"`，**不可**用 `bool(os.getenv(...))`
    （`"0"` / `"false"` 是非空字串，`bool()` 會判 True，等於預設變成開啟——危險）。
  - **subprocess env 繼承**：pipeline manager 若對 stage 傳 `env=`，必須 `env = {**os.environ, ...overrides}`，
    不可只傳 overrides（會抹掉 `PATH` 等基礎環境）。

### §D-invoke Bridge 觸發點
Bridge 是**前置 step**，在 `include_foundry` 模式下於 stage2 之前跑一次（materialize overlay），非綁進 loader。單一 pipeline run 只沙盒重算一次；stage2b/3/5 之後皆讀同一份 per-run 快取（消除「每 stage 各喚醒 docker、值不一致」風險）。實際插入序列（是否成為 `_PIPELINE_SEQUENCE` 一員 vs 獨立前置指令）由 implementation plan 定。

## 6. PIT / OOS discipline

- 重算用**同一份 forge 已驗的碼**（`code_sha256` 校驗）。forge `pit_check_via_sandbox` 已用「汙染未來、斷言過去不變」測過因果性——過閘的因子不會用全樣本統計（`(df-df.mean())/df.std()` 會被抓 `LookaheadError`）。
- 全 span 但 backward-looking，OOS 值不偷看；stage3 `oos_start` 分割照常誠實 train/OOS。
- pre-oos 對帳（§B-2c）是額外的帶：重算 pre-oos == Foundry 存值，否則跳過。
- **所有餵 stage2 選拔的 FactorEntry 指標（IC/IR/regime_ic/stability）皆取自 Foundry pre-oos cards / pre-oos regime_ic**，不從全 span 重算值取任何統計 → 無 OOS 洩漏。

## 6.5 Production 斷層防護（agy 抓出的真洞）

**問題**：本橋只搭研究端。若 stage5 真的選出一個依賴 `foundry_<id>` 因子的策略，人工跑 `promote.py`
之後**仍然上不了線**——live trader 讀 `factor_values_<sym>.parquet`，那是 `scripts/refresh_factors.sh`
**從因子庫的碼**週期性重算出來的。Foundry 因子的碼**不在因子庫裡**，所以 production 永遠不會刷新它
→ 因子立刻 stale → trader 的 `factor data stale:` 護欄直接停機。**選得出卻上不了線的死局。**

（`promote.py` 現行只是把 candidate parquet 的**值**複製進 production features，那是 pre-oos 凍結值，
既不完整也無法隨時間更新——對 live 而言等於死資料。）

**本 spec 的處置**：不在此擴張範圍去做「因子碼進 production 因子庫」，但**必須明確擋住死局，不讓它靜默發生**：

- **Guard**：`promote.py`（或 stage5 選拔輸出處）新增檢查——若策略依賴 `foundry_` 前綴因子，
  而該因子碼**尚未進入 production 因子庫**，則 **REFUSE 並提示**：
  「此策略依賴 Foundry 因子 `<id>`；上線前必須先將其碼納入 production 因子計算路徑（見 follow-on spec）。」
- **Follow-on（另一份 spec）**：`Foundry 因子碼 → production 因子庫`——把 vetted 因子碼註冊進
  stage0a / `refresh_factors` 的計算路徑，使 production 能持續重算它。那才是真正的上線路。

→ 這樣「發掘自動、部署人工閘」的界線仍成立，且**死局被顯性擋下**而非讓使用者撞牆。

## 7. Dedup

**不加庫內去重。** Foundry gate 已對庫內因子去重：`nearest_correlate(factor, existing=features)` + `evaluate` reject if `abs_spearman >= redundant_abs_spearman`（預設 **0.7**）。evidence card 的 `nearest_factor` 就是庫內因子名。0.7 已擋。

## 8. Error handling

- 碼缺 / `code_sha256` 不符 → 跳過該因子記 log。
- pre-oos 對帳失敗（值漂移 / image 漂移）→ 跳過該因子記 log。
- 沙盒重算失敗 → 跳過續跑（橋降級，不炸 pipeline）。
- 撞名 → 跳過記 log。
- 無 candidate → overlay 空，pipeline 照跑庫內因子（no-op）。

## 9. Testing (TDD)

- **§A code persistence**：candidate 因子碼可從 code store 取回；`code_sha256` 相符；graveyard 不寫碼。
- **§B bridge**：
  - 重算產出全 span 值，OOS 區間**非 NaN**。
  - pre-oos 切片對帳：符 → 收；不符 → 跳過。
  - FactorEntry 映射正確；`stability` 呼叫真 `classify_stability`；`verdict` 篩選（只 candidate）。
  - `code_sha256` 不符 → 跳過。
  - 撞名 → 跳過；`reindex_like` + 長度 assert 生效。
  - **NaN 對帳**：pre-oos 切片含 burn-in NaN 時，對帳仍須通過（`equal_nan=True`）——防誤殺合法因子。
  - **Candidate cap**：candidate 數超過 cap → 只取 top-K，overlay 欄數 <= cap。
- **§C loader**：`include_foundry=True` merge 值 + entries；預設關 → production 檔不動、trader 路不受影響；loader 不觸發沙盒。
  - **env 邊界**：`RESEARCH_INCLUDE_FOUNDRY="0"` / `""` / 未設 → 一律**關**（嚴格 `== "1"`）。
  - **撞名不覆寫**：即使發生撞名，loader 的 concat **不得**覆寫 production 既有欄位。
- **§6.5 guard**：策略依賴 `foundry_` 因子且碼未進 production 因子庫 → `promote` REFUSE 並提示。
- **快取清理**：job teardown 後 overlay 檔被刪除，不留孤兒。
- **整合**：`stage2.select_usable_factors` 撿到 foundry entry；`stage3` 解析 `foundry_<id>` 欄跨全 span 回測；stage5 OOS 選拔看得到 foundry 因子 OOS 值。

## 10. Out of scope

- 自動排程（foundry→橋→pipeline 定時觸發）— 另一份 spec（斷点 A）。
- Server root 部署 — 另議（斷点 C）。
- **Foundry 因子碼 → production 因子庫**（真正的上線路）— **另一份 spec**（§6.5 follow-on）。
  本 spec 只負責**擋住死局**，不負責打通 production 計算路徑。
- `promote.py` 人工部署閘本身 — 不變（只加 §6.5 的 refuse guard）。
- runner relative-path mount 可攜性修復 — 另議。

## 11. Files touched

| 檔 | 動作 |
|---|---|
| `research/hermes/forge.py` / `orchestrator.py` | §A 持久化 forge 碼 + metadata |
| `research/hermes/candidate_store.py` | §A code store 讀寫 helper |
| `research/hermes/foundry_bridge.py` | §B 新橋模組 |
| `research/lib/factor_io.py` | §C `load_features`/`load_manifest` 加 `include_foundry` |
| `research/pipeline/stage2b_compile_signal.py`、`stage3_backtest.py` | §C 讀路收斂到 `load_features` |
| `research/factor_regime.py` | §B 複用 `classify_stability`（不改，只 import） |
| `research/hermes/promote.py` | §6.5 guard：依賴 `foundry_` 因子且碼未進 production 因子庫 → REFUSE |
| `dashboard/server/pipeline_manager.py` | §B-6 job teardown 清 overlay 快取；§C subprocess env 正確繼承 |
| `research/tests/…` | §9 測試 |
