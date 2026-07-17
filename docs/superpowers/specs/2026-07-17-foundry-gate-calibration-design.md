# Foundry 偵測門檻校準 — 設計

- 日期：2026-07-17
- 分支：`quant-trading-dashboard`
- 狀態：設計定案，待寫實作計畫
- 二審：agy（Antigravity / Gemini）第七輪

---

## 1. 問題

**這張網的最低偵測門檻是年化 Sharpe 約 5。**

實驗（positive control，2026-07-16，eth pre-oos 22071 bar）：植入已知強度的人工 alpha，直接餵進 `gatekeeper.evaluate()`，掃訊號權重 `w`：

| w | gross_ic | ir | 年化 Sharpe | dsr | turnover | 結果 |
|---|---|---|---|---|---|---|
| 0.02 | −0.0044 | +0.00235 | +0.22 | 1.3e-05 | 0.170 | FAIL weak_ic |
| 0.05 | +0.0608 | +0.02943 | **+2.75** | 0.0278 | 0.168 | **FAIL DSR 0.03 < 0.5** |
| 0.08 | +0.1255 | +0.05403 | +5.06 | 0.518 | 0.164 | PASS |
| 0.12 | +0.2061 | +0.08043 | +7.53 | 0.979 | 0.156 | PASS |

**一個年化 Sharpe +2.75、`gross_ic` 0.061（門檻的 2 倍）、turnover 0.168（真實區間）、扣完成本仍賺錢的因子，被 DSR 斃掉。** 年化 Sharpe 5 以上的東西在任何市場近乎不存在。

**因此：Foundry 兩輪 40 個點子的「零 candidate」、以及整個 pipeline 兩個月的負面結果，在邏輯上無法解讀。** 量到的是工具極限，不是市場極限。

> **方法論教訓**：先前的推論是「證偽能力這麼強（PIT 護欄、entry-lag 審計、look-ahead 偵測全都抓到過真假陽性）還是零結果 → 所以池子裡沒魚」。這個推論是**邏輯跳躍**：證偽能力強只證明護欄會殺東西，不證明系統**找得到**東西。用一堆已知是死的魚，無法證明網子撈得到活魚。**這套系統從沒被證明能找到任何東西 —— 而 positive control 證明了它找不到。**

### 1.1 根源

```
expected_max_sr = sqrt(var_trials) × max_z = 0.0215 × 2.179 = 0.047
→ 要過 DSR 需 per-bar SR > ~0.05 → 年化 ~4.7
```

三個獨立缺陷疊加：

1. **`var_trials` 被成本異質性灌大**。ledger 記的 `sr_per_bar` 是 **net** ir（已扣 `turnover × cost_frac`）。真實 trial 的 turnover 從 0.02 到 0.39（差 20 倍），成本拖累的離散度因此被算進「搜尋的抽樣噪音」。實測 trial std **0.0215** vs 理論抽樣噪音 `sqrt(1/22045) = 0.0067` —— **灌大 3 倍**。DSR 的虛無模型假設 trials 是零 alpha 下的抽樣；我們餵的卻是一堆被成本拖到 −0.078 的爛因子。**爛因子越多，門檻越高，好因子越進不來。**
2. **公式本身不自洽**：`deflated_sharpe` 用**經驗變異數**卻假設**零均值**（`expected_sr=0`），但實測 trial 分佈中位數是 **−0.0185**（成本拖累）。混用兩者沒有統計依據。
3. **`T` 契約違反**：`research/lib/deflated_sharpe.py` 契約寫明 `T` = train-window bar count，`gatekeeper.evaluate` 卻傳 `T=bars_per_year(cfg.interval)` = **8760**，真實訓練窗是 **22046**。

---

## 2. 最大的風險：這份 spec 本身就是 p-hacking 的滑坡

**修 gate = 放寬篩選。** 先前的 Foundry spec 明列「明確不做：動 gate 門檻 —— 那是 p-hacking 的滑坡」。這份 spec 正在做那件事，所以必須先說清楚為什麼它不是。

**差別**：我們**不是**為了讓某個想要的因子過關而調門檻。`rolling_std_stablecoin_supply`（目前最佳線索，t 值 0.75）在修完之後**仍然應該被拒**。我們修的是三個**可獨立指認的統計錯誤**（§1.1），而判準（控制組）**與任何真實因子無關**。

**但這個說法不能只靠嘴。** 「讓 positive control 好看」和「讓 gate 變寬鬆」在數學上是同一件事：只要拿掉 DSR、把 `max_turnover` 拉到 10，最低偵測 Sharpe 立刻掉到 0.5 —— 曲線變漂亮，網卻是破的。**所以 sensitivity 不能單獨當判準**（§4）。

---

## 3. 四個改動

| # | 改動 | 性質 |
|---|---|---|
| 1 | 新增 `gross_ir` 指標：`weights × fwd1` 的 Sharpe，**不扣成本** | 新指標 |
| 2 | DSR 改吃 **gross** SR；ledger 記 `gross_sr_per_bar`；舊的 net-only trial 不進分佈 | 統計修正 |
| 3 | `foundry_dsr` 的 `T` 改傳 **train-window bar 數** | 契約違反修正 |
| 4 | 新增 `net_ir > 0` 為獨立閘 | 補回 DSR 讓出的職責 |

### 3.1 為什麼 DSR 要吃 gross SR（agy 第七輪）

DSR 檢定的是「**這個訊號的預測能力是不是運氣？**」。虛無假設（零 alpha）下，**gross** SR 的期望正是 0 —— 這讓「經驗變異數 + 零均值」重新自洽。

成本是**確定性的、因子專屬的**，不是搜尋的抽樣噪音。把它混進 `var_trials` 就違反了「trials 來自相似分佈」的前提（§1.1 缺陷 1）。

分兩關，職責分離：
- **第一關（統計顯著性）**：gross SR 跑 DSR，null mean = 0 → 「預測力不是運氣」
- **第二關（商業獲利）**：`net_ir > 0` → 「扣完成本還賺錢」

**閘的順序**（`evaluate` 內既有順序 + 新閘的插入點）：
```
redundant → turnover → weak_ic → net_ir<=0 (新) → DSR (改吃 gross)
```
`net_ir > 0` 放在 DSR **之前**：它便宜、確定性、且語意上是「連錢都賺不到就不必談顯著性」。放後面會讓 DSR 去解釋一個商業上已經出局的因子。

**`T` 的定義（消除模糊）**：`T` = **`metrics["n_samples"]`**，即 `factor` 與 `fwd` 對齊後**非 NaN 的配對數**（實測 eth 為 22046），**不是** `len(features)`（22071）。理由：`deflated_sharpe` 用 `T` 算 `sr_std = sqrt((1+0.5·sr²)/(T−1))`，那是 SR 估計的抽樣誤差，其有效樣本數就是**實際參與估計的觀測數**。用 `len(features)` 會把 warmup 的 NaN 也算進去，高估樣本數、低估抽樣誤差。

粗估效果：若 `var_trials` 收斂到理論抽樣噪音 0.0067，則 `expected_max_sr = 0.0067 × 2.179 = 0.0146` → 需要 per-bar SR ~0.015 → **年化約 1.4**。落在合理區間。這是**預估，不是目標** —— 真實值由控制組量出來。

### 3.2 `max_turnover` 不動

初版判斷是「成本已經算在 `net_ir` 裡（`weights×fwd1 − turnover×cost_frac`），再用 turnover 上限斃一次是重複計價」。**這個判斷不完整**（agy 第七輪指正，採納）。

回測的**線性**成本模型抓不到**非線性滑點、市場衝擊、執行延遲**。turnover 0.6 在回測裡扣完手續費也許賺，實盤會吃穿 order book。

**定位改變，數值不動**：它不是「獲利指標的延伸」，是**物理執行上限** —— 防的是回測幻覺。只改 docstring 講清楚。

### 3.3 歷史 trial 作廢，不回填

ledger 現在記 net `sr_per_bar`，既有 39 個 eth trial 沒有 gross SR。**決策：作廢，ledger 重新開始記 `gross_sr_per_bar`。**

理由：那 39 個 trial 是**用壞掉的網撈的**（被 forge KeyError、被錯的 prompt、被汙染的分佈影響），回填後仍是壞樣本。且 `deflated_sharpe` 在 `n < 2` 時回 1.0（不擋），前幾個新 trial 自然寬鬆 —— 這**符合**多重測試的原意：還沒試幾次，本來就不該重罰。

**代價（明寫）**：多重測試債歸零 = 自願忘記已經試過 39 次。這在統計上是**真的損失**，p-hacking 的債是真的。那 39 筆的 EvidenceCard 仍留在 evidence store 可查，只是不進 DSR 的分佈計算。

---

## 4. 反 p-hacking 的憲法：雙向控制

**沒有這一節，這份 spec 就是在拆護欄。**

### 4.1 負控制 — 釘死偽陽性

**素材**：`graveyard_<sym>.parquet` 裡**真實累積的 LLM 因子值**，時間軸**平移**（+90 天 = 2160 bar）。

**為什麼不用合成噪音**（agy 第七輪，採納）：合成噪音的變異數、峰度、尾部特徵與真實 LLM 因子不同。太「乾淨」的負控制會被輕易拒絕 → **95% 那條線變成假護欄** → 一旦放寬門檻，真實的、帶結構性偏差的廢物因子就會漏過去。時間平移的真實因子保有真實的 turnover、分佈與相關性結構，但**物理上不可能預測未來**。

**只能 shift，不能 shuffle**（修正 agy 的一半）：隨機打亂會**摧毀自相關** → turnover 爆表 → 退化成「太乾淨的噪音」的另一種形態，正是這個設計要避免的。保留自相關是這個負控制的全部價值。

**要求**：拒絕率 **≥95%**（型一錯誤 ≤5%）。

### 4.2 正控制 — 量測靈敏度

**素材**：`factor = z(fwd.shift(-1)) × w + smooth_noise × (1−w)`，掃 `w`。

`fwd.shift(-1)` 是因為 `evaluate` 內部會 `factor.shift(entry_lag=1)`。噪音必須**平滑**（24h 滾動），否則 turnover 爆表 —— 初版用白噪音，turnover 0.63~0.83 全被 `max_turnover` 斃，那是**測試的缺陷**，不是 gate 的缺陷。

**輸出**：最低偵測 Sharpe。

### 4.3 兩個控制都關掉 dedup

**傳空的 `existing_and_dead`。**

否則負控制可能因為 `redundant` 被拒 → 拒絕率好看 → **但 DSR 被放寬時完全看不出來**。控制組要隔離**統計閘**（IC / DSR / net_ir），那才是這份 spec 會動到、也才是會被作弊的那道。

### 4.4 為什麼雙向才有效

拆護欄會**立刻讓負控制紅燈**。sensitivity 和 specificity 互相拉扯 → **物理上無法用放寬來作弊**。這讓「修好了」變成一個**可否證的宣稱**，而不是我說了算。

---

## 5. 元件

| 檔案 | 建/改 | 責任 |
|---|---|---|
| `research/hermes/calibration.py` | 建 | `plant_alpha(fwd, w, seed)` / `shift_control(series, bars)` / `measure_gate(...)`。純函式，測試與 CLI 共用 |
| `research/tests/test_hermes_gate_calibration.py` | 建 | 負控制拒絕率、正控制靈敏度 —— **常設迴歸測試** |
| `research/hermes/gatekeeper.py` | 改 | `gross_ir`、`net_ir>0` 閘、`foundry_dsr(T=...)`、`max_turnover` docstring |
| `research/hermes/orchestrator.py` | 改 | ledger 改寫 `gross_sr_per_bar` |

**控制組必須是常設迴歸測試，不是一次性腳本。** 這種洞（網眼大到只能撈鯨魚）存在了整個專案生命週期都沒人發現，靠的是這次偶然做了 positive control。它不該再靠運氣被發現。

---

## 6. 驗收

**不是「最低偵測 Sharpe 要多低」** —— 那可以靠拆護欄達成（§2）。驗收是**兩條同時成立**：

1. 負控制拒絕率 **≥95%**
2. **在上述約束下**，最低偵測 Sharpe 從 ~5 降到 **≤2**

### 6.1 誠實的出口

**若「拒絕率 ≥95%」和「Sharpe ≤2」無法同時達成 → 那是真實發現，不是失敗。** 代表在這個樣本長度（22071 bar）下，這道門檻的統計功效就是這樣。**該誠實記錄並重新評估整個專案的可行性，而不是調參數湊到綠燈。**

### 6.2 一個必須成立的健全性檢查

`rolling_std_stablecoin_supply`（目前最佳線索，per-bar SR 0.00507，t 值 0.75，年化 0.47）**修完之後仍然必須被拒**。

如果它突然過關了，代表我們把門檻拆過頭了 —— **這正是 §2 說的滑坡的具體長相**。

---

## 7. `alpha = 0.05` 的依據

95% 這個數字**是照統計慣例挑的，不是推導出來的**。依據是標準的 α=0.05；agy 指出量化領域因多重檢定嚴重，有時會要求 99%（López de Prado 的高標準），但既然 DSR 本身就是在懲罰多重檢定，5% 型一錯誤是站得住的慣例。

**我不假裝這個數字有更深的來源。**

---

## 8. 明確不做（YAGNI）

| 不做 | 理由 |
|---|---|
| 動 `gross_ic_min` / `redundant_abs_spearman` / `max_turnover` 的**數值** | 這份 spec 修的是統計錯誤，不是門檻鬆緊 |
| 回填舊 trial 的 gross SR | §3.3 已決 |
| 滾動/衰減 trial 窗、按相關性分群算獨立 trial 家族 | agy 第五輪提案。合理但**沒有證據支持它是瓶頸** —— 反事實顯示修掉汙染後 DSR 也只從 0.000045 升到 0.167。留 backlog |
| graveyard 逐輪擠壓搜尋空間 | 真實的內生衰減，但 n 還小不致命。留 backlog |
| 重跑 Foundry | 修完另議。**修完之前重跑沒有意義** |

---

## 9. agy 第七輪記錄

### 採納

| # | agy 意見 | 處置 |
|---|---|---|
| 1 | 負控制改用**時間平移的真實因子**，不要合成噪音（太乾淨 → 95% 變假護欄） | **採納，這是本輪最有價值的一句** → §4.1 |
| 2 | DSR 改吃 **gross SR**，null mean=0；net SR 留作獲利硬指標 | 採納 → §3.1 |
| 3 | `max_turnover` **不該砍**，它是防非線性滑點的物理護欄，不是獲利指標 | **採納，指正了 Claude 的不完整判斷** → §3.2 |
| 4 | `T` bug 必修 | 採納 → §3 改動 3 |
| 5 | α=0.05 合理 | 採納 → §7 |

### 修正 agy

| agy 意見 | 修正 |
|---|---|
| 負控制用「shift **或** shuffle」真實因子 | **shuffle 是錯的**：打亂會摧毀自相關 → turnover 爆表 → 退化成「太乾淨的噪音」，正是這個設計要避免的缺陷。**只能 shift** → §4.1 |

### agy 上一輪被本實驗推翻的判定

| agy 第六輪判定 | 推翻依據 |
|---|---|
| 「證偽能力強 + 零結果 = 池子裡沒魚，建議停損」 | positive control 量出最低偵測門檻是年化 Sharpe ~5 → 兩個月的負面結果**無法解讀**。agy 已接受此更正 |

### Claude 欠 agy 的更正

agy **第五輪就提出「DSR 自我毒化」，Claude 駁回了**，理由是那 4 張 DSR 死因卡的 `ir` 全是負的。**那個理由在事實上正確，但結論錯誤** —— 用「這幾條魚本來就是死的」去證明「網沒問題」是邏輯跳躍。**agy 的直覺對，Claude 的反駁嚴謹但答錯題。** 該做的不是逐條檢查死掉的因子，而是直接量網眼大小（positive control）。
