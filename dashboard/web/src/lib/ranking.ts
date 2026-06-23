import type { StrategyRow } from "./api";

export type SortKey = "rank" | "sharpe_oos" | "dd_oos" | "trades_oos" | "sharpe" | "pipeline_stage";
export type SortDir = "asc" | "desc";

// Deployability tier: GO (gate pass & not fatal) = 0; non-fatal non-GO = 1; fatal = 2.
function deployTier(r: StrategyRow): number {
  if (r.gate_fatal === true) return 2;
  if (r.gate_pass === true) return 0;
  return 1;
}

// Compare two nullable numbers; null ALWAYS sinks regardless of direction.
function cmpNum(a: number | null, b: number | null, dir: SortDir): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return dir === "asc" ? a - b : b - a;
}

// Default "deployability" rank: GO first -> OOS Sharpe desc (null last) -> fatal bottom.
export function defaultRank(a: StrategyRow, b: StrategyRow): number {
  const tier = deployTier(a) - deployTier(b);
  if (tier !== 0) return tier;
  return cmpNum(a.sharpe_oos, b.sharpe_oos, "desc");
}

// Column-header sort. null always sinks. Returns a comparator.
export function compareBy(key: SortKey, dir: SortDir): (a: StrategyRow, b: StrategyRow) => number {
  if (key === "rank") return defaultRank;
  if (key === "pipeline_stage") {
    return (a, b) => (dir === "asc" ? a.pipeline_stage - b.pipeline_stage : b.pipeline_stage - a.pipeline_stage);
  }
  const get = (r: StrategyRow): number | null =>
    key === "sharpe_oos" ? r.sharpe_oos
    : key === "dd_oos" ? r.dd_oos
    : key === "trades_oos" ? r.trades_oos
    : r.sharpe;
  return (a, b) => cmpNum(get(a), get(b), dir);
}
