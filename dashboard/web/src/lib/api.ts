const BASE = "/api";

async function get<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

/**
 * True when a fetch rejected because its AbortController was aborted (the poll
 * tick was superseded by a newer one, or the component unmounted). Callers
 * should swallow these — they are not real errors and must not overwrite the
 * UI with a stale-request failure message.
 */
export const isAbortError = (e: unknown): boolean =>
  e instanceof DOMException && e.name === "AbortError";

// ---------------------------------------------------------------------------
// Enums (mirrors schemas.py)
// ---------------------------------------------------------------------------

export type FactorVerdict = "single_use" | "ensemble_only" | "reject";
export type FactorStability = "regime_stable" | "conditional";
export type RecommendedAction = "proceed" | "back_to_stage_2" | "back_to_stage_4";
export type RedFlagCode =
  | "oos_sharpe_far_below_is"
  | "underperforms_hodl"
  | "too_few_trades"
  | "alpha_is_fee_illusion"
  | "overfit_suspect"
  | "regime_conditional";
export type LiveStatus = "running" | "paused" | "stopped";
export type AlertSeverity = "info" | "warning" | "critical";

// ---------------------------------------------------------------------------
// Factor manifest — factor_<symbol>.json
// ---------------------------------------------------------------------------

export interface FactorEntry {
  name: string;
  ic_by_horizon: Record<number, number | null>;
  ir: number;
  sample_size: number;
  cross_regime_ic: Record<string, number | null> | null;
  stability: FactorStability | null;
  verdict: FactorVerdict;
}

export interface FactorManifest {
  schema_version: number;
  symbol: string;
  generated_at: string;
  period_days: number;
  horizons_h: number[];
  factors: FactorEntry[];
}

// ---------------------------------------------------------------------------
// Strategy manifest — <strategy_id>/manifest.json
// ---------------------------------------------------------------------------

export interface BacktestMetrics {
  source_run: string;
  sharpe: number | null;
  max_drawdown: number | null;
  trades: number | null;
  profit_factor: number | null;
  total_return: number | null;
  win_rate: number | null;
}

export interface WalkForwardWindow {
  window: string;
  sharpe: number | null;
  total_return: number | null;
}

export interface WalkForwardBlock {
  source_run: string;
  windows: WalkForwardWindow[];
}

export interface MonteCarloBlock {
  source_run: string;
  n_simulations: number | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_crosses_zero: boolean | null;
}

export interface BenchmarkBlock {
  source_run: string;
  strategy_return: number | null;
  hodl_return: number | null;
  excess_return: number | null;
  beats_hodl: boolean | null;
}

export interface RegimeMetrics {
  regime: string;
  source_run: string;
  sharpe: number | null;
  max_drawdown: number | null;
  total_return: number | null;
  trades: number | null;
}

export interface CostStressLevel {
  label: string;
  source_run: string;
  fee_multiplier: number;
  sharpe: number | null;
  total_return: number | null;
  profit_factor: number | null;
}

export interface CostStressBlock {
  source_run: string;
  levels: CostStressLevel[];
}

export interface BacktestBlock {
  in_sample: BacktestMetrics;
  oos: BacktestMetrics | null;
  walk_forward: WalkForwardBlock | null;
  monte_carlo: MonteCarloBlock | null;
  benchmark: BenchmarkBlock | null;
  by_regime: RegimeMetrics[];
  cost_stress: CostStressBlock | null;
}

export interface GateThreshold {
  name: string;
  threshold: number;
  actual: number | null;
  passed: boolean;
  fatal: boolean;
}

export interface GateBlock {
  source_run: string | null;
  thresholds: GateThreshold[];
  overall_pass: boolean;
  fatal_fail: boolean;
  red_flags: RedFlagCode[];
}

export interface SpecBlock {
  source_run: string | null;
  strategy_id: string;
  symbol: string;
  spec_yaml: string;
  description: string | null;
}

export interface GenerationBlock {
  source_run: string | null;
  method: string;
  model: string | null;
  rationale: string | null;
  factors_used: string[];
}

export interface ReproducibilityBlock {
  source_run: string | null;
  git_commit: string | null;
  config_hash: string | null;
  engine: string | null;
  data_source: string | null;
  seed: number | null;
}

export interface DiagnosisBlock {
  source_run: string | null;
  recommended_action: RecommendedAction;
  summary: string | null;
  findings: string[];
}

export interface OptimizationBlock {
  source_run: string | null;
  method: string | null;
  swept_params: string[];
  best_params: Record<string, number>;
  improvement_summary: string | null;
}

export interface StrategyManifest {
  schema_version: number;
  strategy_id: string;
  symbol: string;
  generated_at: string;
  pipeline_stage: number;
  spec: SpecBlock;
  generation: GenerationBlock | null;
  reproducibility: ReproducibilityBlock | null;
  backtest: BacktestBlock | null;
  optimization: OptimizationBlock | null;
  diagnosis: DiagnosisBlock | null;
  gate: GateBlock | null;
}

// ---------------------------------------------------------------------------
// Selection manifest
// ---------------------------------------------------------------------------

export interface SelectionEntry {
  strategy_id: string;
  symbol: string;
  rank: number;
  score: number;
  selected: boolean;
}

export interface SelectionManifest {
  schema_version: number;
  generated_at: string;
  method: string | null;
  ranking: SelectionEntry[];
}

// ---------------------------------------------------------------------------
// Testnet status — runs/testnet/<id>/testnet_status.json
// ---------------------------------------------------------------------------

export interface LiveBlock {
  started_at: string;
  updated_at: string;
  status: LiveStatus;
  equity: number | null;
  open_positions: number;
  trades: number;
  sharpe: number | null;
  max_drawdown: number | null;
}

export interface VsBacktestBlock {
  live_sharpe: number | null;
  backtest_sharpe: number | null;
  sharpe_ratio: number | null;
  live_slippage: number | null;
  assumed_slippage: number | null;
  unfilled_orders: number;
}

export interface KillswitchBlock {
  triggered: boolean;
  triggered_at: string | null;
  reason: string | null;
  pause_drawdown: number;
  terminate_drawdown: number;
}

export interface TestnetAlert {
  timestamp: string;
  severity: AlertSeverity;
  message: string;
}

export type TradingMode = "paper" | "testnet" | "live";

export interface TestnetStatus {
  schema_version: number;
  testnet_id: string;
  strategy_id: string;
  symbol: string;
  mode: TradingMode | null;
  live: LiveBlock;
  vs_backtest: VsBacktestBlock | null;
  killswitch: KillswitchBlock;
  alerts: TestnetAlert[];
}

// Live trader CSV rows (runs/testnet/<id>/equity.csv, trades.csv)
export interface TestnetEquityRow {
  timestamp: string;
  equity: number;
}

export interface TestnetTradeRow {
  timestamp: string;
  symbol: string;
  side: string;
  qty: number;
  price: number;
}

// ---------------------------------------------------------------------------
// Simplified list types (what /api/strategies returns)
// ---------------------------------------------------------------------------

export interface StrategyRow {
  strategy_id: string;
  symbol: string;
  pipeline_stage: number;
  generated_at: string;
  gate_pass: boolean | null;
  gate_fatal: boolean | null;
  sharpe: number | null;        // in-sample (secondary)
  max_drawdown: number | null;  // in-sample (secondary)
  sharpe_oos: number | null;    // OOS / walk-forward (primary ranking metric)
  dd_oos: number | null;
  trades_oos: number | null;
  pf_oos: number | null;
  red_flags: RedFlagCode[];
  interval: string;
}

export interface PipelineRow {
  strategy_id: string;
  symbol: string;
  pipeline_stage: number;
  generated_at: string;
}

// ---------------------------------------------------------------------------
// Equity / trades (csv→JSON)
// ---------------------------------------------------------------------------

export interface EquityPoint {
  time: string;
  equity: number;
  drawdown: number;
}

// ---------------------------------------------------------------------------
// Pipeline status — GET /api/pipeline/status
// ---------------------------------------------------------------------------

export type StageState = "done" | "stale" | "missing";

export interface StageStatus {
  stage_id: string;
  label: string;
  state: StageState;
  generated_at: string | null;
  metric_label: string | null;
  metric_value: string | null;
}

export interface StrategyPipeline {
  strategy_id: string;
  stages: StageStatus[];
}

export interface SymbolPipeline {
  symbol: string;
  stages: StageStatus[];
  strategies: StrategyPipeline[];
  runnable: boolean;   // false = disk-only symbol not in research_config; Run disabled
}

export interface PipelineStatus {
  generated_at: string;
  symbols: SymbolPipeline[];
}

// ---------------------------------------------------------------------------
// Pipeline jobs — A2 run/jobs/cancel
// ---------------------------------------------------------------------------

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "canceled";

export interface JobStep {
  stage: string;
  status: "pending" | "running" | "succeeded" | "failed" | "skipped";
  exit_code: number | null;
}

export interface PipelineJob {
  job_id: string;
  kind: "stage" | "pipeline";
  stage: string | null;
  symbol: string | null;
  interval: string;
  status: JobStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  steps: JobStep[];
  exit_code: number | null;
  error: string | null;
  cancel: boolean;
  stress: boolean;
}

export interface PipelineJobDetail extends PipelineJob {
  log_tail: string;
}

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------

export const api = {
  strategies: (interval = "1H"): Promise<StrategyRow[]> => get(`/strategies?interval=${interval}`),
  strategy: (id: string, interval = "1H"): Promise<StrategyManifest> =>
    get(`/strategies/${id}?interval=${interval}`),
  equity: (id: string, run?: string): Promise<EquityPoint[]> =>
    get(`/strategies/${id}/equity${run ? `?run=${run}` : ""}`),
  trades: (id: string, run?: string): Promise<Record<string, unknown>[]> =>
    get(`/strategies/${id}/trades${run ? `?run=${run}` : ""}`),
  factorAnalysis: (interval = "1H"): Promise<FactorManifest[]> =>
    get(`/factor-analysis?interval=${interval}`),
  regime: (symbol: string, interval = "1H"): Promise<Record<string, unknown>> =>
    get(`/regime?symbol=${symbol}&interval=${interval}`),
  selection: (interval = "1H"): Promise<SelectionManifest> => get(`/selection?interval=${interval}`),
  intervals: (): Promise<string[]> => get("/intervals"),
  pipeline: (): Promise<PipelineRow[]> => get("/pipeline"),
  testnet: (init?: RequestInit): Promise<TestnetStatus[]> => get("/testnet", init),
  testnetDetail: (id: string): Promise<TestnetStatus> => get(`/testnet/${id}`),
  testnetEquity: (id: string): Promise<TestnetEquityRow[]> =>
    get(`/testnet/${encodeURIComponent(id)}/equity`),
  testnetTrades: (id: string): Promise<TestnetTradeRow[]> =>
    get(`/testnet/${encodeURIComponent(id)}/trades`),
  promoteStatus: (
    id: string,
  ): Promise<{
    strategy_id: string;
    promoted: boolean;
    running: boolean;
    running_testnet_id: string | null;
    running_mode: string | null;
  }> => get(`/strategies/${id}/promote`),
  promote: (id: string, body: { override_reason?: string }) =>
    fetch(`${BASE}/strategies/${id}/promote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  demote: (id: string) =>
    fetch(`${BASE}/strategies/${id}/promote`, { method: "DELETE" }),
  traderStart: (
    testnetId: string,
    body: {
      strategy_id: string;
      run_dir?: string;
      symbol?: string;
      interval?: string;
      qty?: number;
      mode?: TradingMode;
    },
  ) =>
    fetch(`${BASE}/testnet/${testnetId}/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  traderStop: (testnetId: string, strategyId: string) =>
    fetch(`${BASE}/testnet/${testnetId}/stop?strategy_id=${encodeURIComponent(strategyId)}`, {
      method: "POST",
    }),
  traderProcess: (testnetId: string, strategyId: string): Promise<{ running: boolean; pid: number | null }> =>
    get(`/testnet/${testnetId}/process?strategy_id=${encodeURIComponent(strategyId)}`),
  getPipelineStatus: () => get<PipelineStatus>("/pipeline/status"),
  runPipeline: (body: { kind: "stage" | "pipeline"; stage?: string; symbol?: string; stress?: boolean; interval?: string }): Promise<PipelineJob> =>
    fetch(`${BASE}/pipeline/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(async (r) => {
      if (!r.ok) {
        // Surface FastAPI's `detail` (e.g. "unknown symbol 'sol'") instead of a
        // bare "400 Bad Request" that hides why the run was rejected.
        const detail = await r.json().then((b) => b?.detail).catch(() => null);
        throw new Error(detail ? `${r.status} ${detail}` : `${r.status} ${r.statusText}`);
      }
      return r.json();
    }),
  listPipelineJobs: (init?: RequestInit) => get<PipelineJob[]>("/pipeline/jobs", init),
  getPipelineJob: (id: string, init?: RequestInit) => get<PipelineJobDetail>(`/pipeline/jobs/${id}`, init),
  cancelPipelineJob: (id: string): Promise<PipelineJob> =>
    fetch(`${BASE}/pipeline/jobs/${id}/cancel`, { method: "POST" }).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    }),
};
