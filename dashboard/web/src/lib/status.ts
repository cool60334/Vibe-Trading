import type { StrategyRow } from "./api";

// ---------------------------------------------------------------------------
// Deployment-status bucket
// ---------------------------------------------------------------------------
//
// One bucket per strategy, derived only from fields already on StrategyRow.
// Order matters: a FATAL strategy also has gate_pass === false, so the fatal
// check must come before the pass check.
//
//   running    — a trader is live on it (running_mode set)  (operational truth)
//   dead       — gate_fatal === true                (hard block, archive)
//   incomplete — no gate block yet (gate_pass null)  (stalled pre-backtest)
//   deployable — gate passed, not fatal              (GO)
//   iterate    — gate ran, NO-GO, not fatal          (fixable)
//
// `running` is checked first: a live paper/testnet strategy is the operational
// truth and must surface as running even if its manifest lacks a gate (which
// would otherwise bucket it as `incomplete`/N/A — e.g. a CLI-deployed strategy).

export type StatusBucket = "running" | "deployable" | "iterate" | "dead" | "incomplete";

export function statusBucket(r: StrategyRow): StatusBucket {
  if (r.running_mode) return "running";
  if (r.gate_fatal === true) return "dead";
  if (r.gate_pass === null) return "incomplete";
  if (r.gate_pass === true) return "deployable";
  return "iterate";
}

// Render order + per-bucket presentation. `defaultOpen` drives which groups
// start expanded (running + deployable + iterate); dead/incomplete start collapsed.
export const BUCKET_ORDER: StatusBucket[] = ["running", "deployable", "iterate", "dead", "incomplete"];

export interface BucketMeta {
  label: string;
  hint: string;
  defaultOpen: boolean;
}

export const BUCKET_META: Record<StatusBucket, BucketMeta> = {
  running: { label: "運行中", hint: "Paper / Testnet / Live 模擬中", defaultOpen: true },
  deployable: { label: "可部署", hint: "通過 gate、非致命", defaultOpen: true },
  iterate: { label: "待優化", hint: "已評估、NO-GO，可迭代", defaultOpen: true },
  dead: { label: "淘汰", hint: "致命（FATAL）", defaultOpen: false },
  incomplete: { label: "未完成", hint: "沒跑回測 / 無 gate", defaultOpen: false },
};

// ---------------------------------------------------------------------------
// Next-step suggestion chip
// ---------------------------------------------------------------------------

export type ChipTone = "success" | "warning" | "neutral" | "danger" | "info";

export interface NextStep {
  label: string;
  tone: ChipTone;
}

export function nextStep(r: StrategyRow): NextStep {
  const bucket = statusBucket(r);
  switch (bucket) {
    case "running": {
      const mode = r.running_mode ?? "paper";
      const label = mode.charAt(0).toUpperCase() + mode.slice(1);
      // Safety: a strategy that is live yet has a fatal gate must not look healthy.
      // It should never have been deployed — flag it so the user can kill it.
      if (r.gate_fatal === true) return { label: `FATAL 卻在跑 (${label})`, tone: "danger" };
      return { label: `${label} 監控中`, tone: "success" };
    }
    case "deployable":
      return { label: "跑 paper-forward", tone: "success" };
    case "dead":
      return { label: "封存", tone: "danger" };
    case "incomplete":
      return { label: "跑 Stage 3 回測", tone: "info" };
    case "iterate":
      if (r.recommended_action === "back_to_stage_2")
        return { label: "回 Stage 2 改 archetype", tone: "neutral" };
      if (r.recommended_action === "back_to_stage_4")
        return { label: "重跑 Stage 4 優化", tone: "warning" };
      return { label: "檢視 gate", tone: "warning" };
    default: {
      // Exhaustiveness guard: if a new StatusBucket is added without a branch
      // here, this assignment fails to compile.
      const _exhaustive: never = bucket;
      return _exhaustive;
    }
  }
}
