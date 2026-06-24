export type RiskZone = "safe" | "caution" | "danger";

export interface RiskHeadroom {
  zone: RiskZone;
  currentPct: number;     // current DD as % (positive)
  pausePct: number;
  terminatePct: number;
  toPause: number;        // pause - current (fraction; negative once past)
  toTerminate: number;    // terminate - current
  fillFraction: number;   // current / terminate, clamped 0..1 (gauge fill)
}

const clamp01 = (x: number): number => Math.max(0, Math.min(1, x));

/**
 * Risk headroom of a live trader. All inputs are positive drawdown fractions
 * (e.g. 0.04 / 0.05 / 0.07). `currentDd` must be the authoritative current DD
 * (status.live.max_drawdown) — never recomputed from a truncated equity window.
 */
export function riskHeadroom(
  currentDd: number,
  pauseDd: number,
  terminateDd: number,
): RiskHeadroom {
  const zone: RiskZone =
    currentDd >= terminateDd ? "danger" : currentDd >= pauseDd ? "caution" : "safe";
  return {
    zone,
    currentPct: currentDd * 100,
    pausePct: pauseDd * 100,
    terminatePct: terminateDd * 100,
    toPause: pauseDd - currentDd,
    toTerminate: terminateDd - currentDd,
    fillFraction: terminateDd > 0 ? clamp01(currentDd / terminateDd) : 0,
  };
}
