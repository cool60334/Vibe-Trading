import type { StrategyRow } from "./api";

// ---------------------------------------------------------------------------
// Deployment-status bucket
// ---------------------------------------------------------------------------
//
// One bucket per strategy, derived only from fields already on StrategyRow.
// Order matters: a FATAL strategy also has gate_pass === false, so the fatal
// check must come before the pass check.
//
//   dead       — gate_fatal === true                (hard block, archive)
//   incomplete — no gate block yet (gate_pass null)  (stalled pre-backtest)
//   deployable — gate passed, not fatal              (GO)
//   iterate    — gate ran, NO-GO, not fatal          (fixable)

export type StatusBucket = "deployable" | "iterate" | "dead" | "incomplete";

export function statusBucket(r: StrategyRow): StatusBucket {
  if (r.gate_fatal === true) return "dead";
  if (r.gate_pass === null) return "incomplete";
  if (r.gate_pass === true) return "deployable";
  return "iterate";
}

// Render order + per-bucket presentation. `defaultOpen` drives which groups
// start expanded (deployable + iterate); dead/incomplete start collapsed.
export const BUCKET_ORDER: StatusBucket[] = ["deployable", "iterate", "dead", "incomplete"];

export interface BucketMeta {
  label: string;
  hint: string;
  defaultOpen: boolean;
}

export const BUCKET_META: Record<StatusBucket, BucketMeta> = {
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
