## Context

現有 pipeline:stage0(`crypto_factor_lab` swarm)盲提案 `FactorCandidate`(限 `SOURCE_REGISTRY` 4 源 × `TRANSFORM_REGISTRY` 8 變換),`factor_proposer` prompt 寫死 funding 反向先驗;stage1(`factor_extended.py`)`_compute_candidate_series` 才 re-fetch + transform 算 Spearman IC 與 verdict。stage1 已用 `lib/factor_io.dump_factor_values` 寫 `factor_values_<sym>.parquet`,下游 stage2b 編出的 `signal_engine` 已透過 `load_factor_values` 按因子名讀 parquet —— **feature store 機制已是骨幹**。

約束:平台不支援 Anthropic 直連(走 OpenRouter);Windows 環境(TA-Lib 編譯痛);記憶顯示 LLM 做確定性工作曾燒 445k token / 陷編輯死循環;狹窄因子空間 + 寫死先驗導致過擬合死路。

## Goals / Non-Goals

**Goals:**
- 因子發掘改為證據驅動:agent 提案前先有真實 IC 證據可看。
- 拿掉寫死先驗,prompt 改方法紀律護欄,擴大搜尋空間。
- 支援 agent 自由發明跨源複合因子,且下游可重現取值。
- 確定性工作(JSON 格式化、合規驗證)交回程式,降低 LLM token 燒耗與死循環風險。

**Non-Goals:**
- 不安裝 freqtrade 本體、不用其 backtest/hyperopt(回測仍自寫 stage2/3/4)。
- 不改下游 stage2/2b/3/4/5、signal_engine、dashboard。
- 不引入 ML 模型(維持規則式因子)。
- 非價格資料源(funding/OI/stablecoin)不改 fetcher。

## Decisions

**D1. Feature store(擴充現有)而非擴充 registry/DSL。**
延伸已是骨幹的 `factor_values` parquet 機制:pre-compute 整批指標 + agent 驗證過的複合因子皆寫成具名欄,下游按名讀、不重算。理由:(1) 低風險(延伸既有,非新建);(2) 唯一能支撐 agent 自由發明跨源複合因子的路;(3) 重算邏輯集中於一處。替代案「擴充 registry」每加一指標就要改 registry + stage1 硬編 + schema Literal,且鎖死單一 `(source,transform)` 配對、無法跨源複合 —— 與目標衝突,否決。

**D2. pandas-ta 而非完整 freqtrade。**
OHLCV 已由現有 ccxt fetcher 解決,freqtrade download-data 邊際價值低;真正要的指標庫由純 python 的 pandas-ta(~130 指標)全包,免 TA-Lib 編譯痛;完整 freqtrade 還拖回測機器(已宣告不用)。替代案:完整 freqtrade(依賴最重)、ccxt-only 無指標庫(達不到「各類指標」)。日後真需 freqtrade 本體,download-only 是升級路。

**D3. 混合模式 agent(確定性 IC 表 + bash 工具)。**
程式先確定性算好整批指標 IC 排名表交給 researcher;researcher 另有 bash 工具讀 features parquet、可自算複合因子並回測 IC 後再定案。理由:重 IC 計算走確定性程式(可重現、省 token),保留 agent 自由探索與複合創造。替代案:純 curator(只讀表不能試,彈性不足)、全自主(agent 自算一切,token/死循環風險高 —— 記憶教訓)。

**D4. swarm 改 2 agents,移除 LLM formatter。**
原 critic 的「合規檢查」職責已被 pre-compute + feature store 自動化,重定位為 `skeptic`(過擬合/正交/經濟邏輯);原 formatter 的「吐 JSON」是確定性工作,改由 stage0 既有 `parse_candidates_json` + pydantic 處理。理由:減一個易燒 token / 死循環的 LLM 環節,同時保留對抗式過擬合審查(過擬合是已知頭號殺手)。

**D5. proposal prompt = 方法紀律護欄。**
不告訴方向/答案,但保留研究紀律:禁 look-ahead/資料洩漏、要求經濟邏輯、偏好低相關正交因子、對過高 IC 保持懷疑、誠實回報 IC。理由:拔除人為先驗偏誤(ETH 死路根因),但不放任過擬合。

**D6. `feature_key` 作為契約;stage1 不再 re-fetch。**
`FactorCandidate` 新增 `feature_key`,stage1 改 `load_factor_values`/features store 按 key 取序列。`data_source`/`transform` 降為選填說明性以向後相容舊 manifest。stage0 驗證由「比對 registry」改為「`feature_key` 須存在於 feature store 且覆蓋率達門檻」。

**D7. agent 複合因子 append 約定。**
researcher 自算的複合因子寫進 staging(例 `features_<sym>.staging.parquet` 或同檔新增欄),stage0 接受 candidate 前驗證該 `feature_key` 已存在於 store 且非全 NaN;不存在則拒收該 candidate。確保下游可重現。

## Risks / Trade-offs

- **pandas-ta numpy 2.0 相容雷** → mitigation:pin numpy<2 或退 `ta` 套件;CI 測指標計算冒煙。
- **大指標池 → multiple-testing / IC 膨脹偽訊號** → mitigation:evidence 表僅作篩選,真正 gate 仍靠 skeptic + stage1 verdict;evidence 表標註 caveat,並可記錄檢定數供 FDR 參考。
- **agent 寫垃圾序列進 feature store** → mitigation:staging + stage0 接受前驗證(存在性 + 覆蓋率門檻 + 非全 NaN);schema pydantic 驗證。
- **BREAKING schema 改動破壞舊 manifest** → mitigation:`data_source`/`transform` 改選填、`feature_key` 過渡期可由舊欄推導;舊 manifest 仍能驗證載入。
- **forward-fill 自相關膨脹 IR(既有 caveat)** → 沿用既有 caveat 文字,不退化。
- **agent 複合因子引入 look-ahead** → mitigation:prompt 護欄明令因果計算(僅 rolling/過去窗)+ skeptic 審查;指標池本身用因果 rolling。

## Migration Plan

採加法式、可回退:
1. 加 `feature-evidence-build`(stage0a)+ `factor-values-io` 擴充 + schema 加 `feature_key`(全加法,不破壞既有)。
2. 切換 `crypto_factor_lab.yaml` 為 2-agent 證據驅動。
3. stage1 切到 feature store 取值路徑。
4. BTC 跑 end-to-end 驗證,回歸 stage2b。

回退:過渡期保留 `RESEARCH_LEGACY_FACTORS` 既有 legacy 路徑與舊 registry 驗證(behind flag);新流程出問題可切回 legacy。

## Open Questions

- 指標池確切清單由 config 預設(動量/趨勢/波動/量),首版範圍是否足夠,留待 BTC 實測後微調。
- agent 複合因子 staging 採「獨立 staging parquet」或「直接 append 主 features parquet」,實作時依 factor_io 最小改動決定。
