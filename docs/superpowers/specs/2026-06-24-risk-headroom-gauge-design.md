# 距 Kill 風險餘裕 Gauge（Risk Headroom Gauge）— 設計

- **日期**：2026-06-24
- **分支**：quant-trading-dashboard
- **狀態**：設計已核准（agy 審 + 自驗 against trader code），待寫實作計畫
- **脈絡**：P3 前端 — 深化 Testnet 實盤監控頁。純前端、無 schema/backend 改。

---

## 1. 背景

`dashboard/web/src/pages/Testnet.tsx` 的 Testnet 頁已成熟（Tier 2：mode badge / live metrics / 即時 equity 曲線 / 成交 / vs-backtest / kill-switch 閾值 / alerts / 15s 輪詢）。但 `KillSwitchPanel` 只印 `pause_drawdown` + `terminate_drawdown` 兩個**閾值數字**，看不出**當前離暫停/爆倉多近**。live trader（eth_s5 + sol_s1）最核心的「我安不安全」訊號缺席。

## 2. 承重事實（agy 審 + 已對 trader code 驗證）

- `dashboard/trader/killswitch.py:87` `KillSwitch.current_drawdown()` 回**當前 DD 正分數** `max(0,(peak−eq)/peak)`，peak 是**持久化的全期高點**（`killswitch_state.json`，line 37-50/66-68，跨重啟保留）。
- `dashboard/trader/loop.py:277` `max_dd = ks.current_drawdown(equity)` → 寫進 status 的 `max_drawdown` 欄。
- ⇒ **`live.max_drawdown` 其實 = 當前 DD**（欄名誤導），且**正是 kill-switch `check()` 觸發用的同一個 `(peak−eq)/peak`**。

**結論（agy 修正、已驗）**：gauge 必須直接消費 `live.max_drawdown`（= 權威當前 DD），**不可在前端用 equity 尾端自算 DD** —— 前端只看到截斷窗，若真高點早於可見窗（或跨重啟），自算會用偏低的 local peak → **低估離 kill 的接近度**＝風險 gauge 的致命 bug。

---

## 3. 目標 / 非目標

### 目標
- 每個 trader 一條 gauge：當前 DD vs pause/terminate 閾值，一眼看風險餘裕。
- 文字：距暫停 / 距終止各還有多少。
- 色態：green(<pause) / amber(pause~terminate) / red(≥terminate 或 killswitch.triggered)。

### 非目標（YAGNI）
- 任何 schema/backend 改。
- 前端自算 DD / lift equity fetch（v1 的錯，已棄）。
- 捏造 historical max DD 參考刻度（backend 不發、不假造）。
- rename `max_drawdown` 誤導欄名（另案、會 ripple，低優先）。

---

## 4. 設計（純前端，強化 `KillSwitchPanel`）

### 4.1 純 util `dashboard/web/src/lib/risk.ts`

```ts
export type RiskZone = "safe" | "caution" | "danger";

export interface RiskHeadroom {
  zone: RiskZone;
  currentPct: number;     // 當前 DD %（正）
  pausePct: number;
  terminatePct: number;
  toPause: number;        // pause - current（分數；負=已過）
  toTerminate: number;    // terminate - current
  fillFraction: number;   // current / terminate，clamp 0..1（gauge 填充比）
}

// currentDd / pauseDd / terminateDd 皆正分數（如 0.04 / 0.05 / 0.07）。
export function riskHeadroom(
  currentDd: number, pauseDd: number, terminateDd: number,
): RiskHeadroom;
```

zone 規則：`current ≥ terminate → danger`；`current ≥ pause → caution`；否則 `safe`。
`fillFraction = clamp(current / terminate, 0, 1)`。`toPause = pause − current`、`toTerminate = terminate − current`。
`currentDd` 為 `null`（trader 還沒寫 equity）時，元件顯示「等待資料」，不畫 marker（util 只吃 number；null 由元件處理）。

### 4.2 元件 `RiskGauge`（放 `Testnet.tsx` 內，KillSwitchPanel 旁）

水平 bar，0 → terminate 為刻度全長：
- 背景三段：green 區 [0,pause)、amber 區 [pause,terminate)、red 區端點。
- pause / terminate 兩條刻度線 + 標籤。
- 當前 DD marker（▼ 或實心點）落在 `fillFraction` 位置，顏色依 zone。
- 下方文字：`當前 DD a.aa% · 距暫停 b.bb% · 距終止 c.cc%`（已過則標紅「已逾」）。
- `killswitch.triggered` 為真 → marker/邊框紅、沿用現有 triggered banner（不重複）。

### 4.3 `KillSwitchPanel` 強化

現有兩個閾值卡保留（或併入 gauge 標籤）；在其上方插入 `RiskGauge`：
```
<KillSwitchPanel ks={killswitch} currentDd={live.max_drawdown} />
  → RiskGauge(currentDd, ks.pause_drawdown, ks.terminate_drawdown, ks.triggered)
  → 既有閾值卡
  → 既有 triggered banner
```
資料全在 `TestnetCard` 已有的 `status.live` + `status.killswitch`，**不需 card 重構、不需新 fetch**。

---

## 5. 測試

- `dashboard/web` **無 vitest**（僅 dev/build/preview）→ 不加測試框架（YAGNI）。`riskHeadroom` 靠 `tsc -b` 型別 + preview 行為驗。
- **preview 驗證**：起 dev server（+ backend）→ Testnet 頁 → 截圖確認 gauge 三色區、marker 落點、headroom 文字；測幾個 DD 值（safe/caution/danger）的呈現；triggered 態。
- 邊界：`currentDd=null`（無 equity）→ 顯示等待、不畫 marker；`current > terminate` → marker clamp 在端點 + 文字標「已逾」。

---

## 6. 成功標準

開 Testnet 頁的 live trader 卡 →

1. KillSwitch 區出現風險 gauge：當前 DD（取自 `live.max_drawdown`）相對 pause/terminate 兩線的位置 + 色態。
2. 文字明示距暫停/距終止餘裕。
3. 數字與 kill-switch 實際觸發用的 DD **完全一致**（直接消費同一欄、無前端再算）。
4. `tsc -b` + build 綠、preview 截圖佐證（含 safe/caution/danger/triggered 各態）。

---

## 7. 已定決策

- 直接用 `live.max_drawdown`（= 權威當前 DD、kill-switch 同源），**不前端自算**（agy 審 + killswitch.py:87/loop.py:277 驗）。
- 純前端、無 schema/backend 改、無 card 重構。
- 無 vitest → tsc + preview 驗（不加框架）。
- 強化既有 `KillSwitchPanel`、不開新頁/新 panel（DRY）。
- 連 [[project_mainnet_paper_dryrun]]（Tier 2 監控基建）、[[deploy_dashboard_testnet_runbook]]（KILL_TERMINATE_DD）。
