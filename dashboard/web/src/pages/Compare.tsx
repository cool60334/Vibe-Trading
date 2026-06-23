import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type StrategyRow, type RedFlagCode } from "../lib/api";
import { compareBy, type SortKey, type SortDir } from "../lib/ranking";
import { useInterval } from "@/hooks/useInterval";
import { cn } from "../lib/utils";

// ---------------------------------------------------------------------------
// PipelineStrip
// ---------------------------------------------------------------------------

const STAGE_LABELS = ["因子", "策略", "回測", "優化", "選擇"] as const;

function PipelineStrip({ rows }: { rows: StrategyRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="mb-4 rounded-lg border bg-card p-3">
      <div className="text-xs font-medium text-muted-foreground mb-2">Pipeline 進度</div>
      <div className="flex flex-col gap-1.5">
        {rows.map((r) => (
          <div key={`${r.interval}_${r.strategy_id}`} className="flex items-center gap-2 text-xs">
            <span className="w-32 truncate font-mono text-foreground">{r.strategy_id}</span>
            <div className="flex gap-1">
              {STAGE_LABELS.map((label, i) => (
                <div
                  key={i}
                  title={`Stage ${i + 1}: ${label}`}
                  className={cn(
                    "h-3 w-8 rounded-sm text-center leading-3",
                    r.pipeline_stage > i
                      ? "bg-emerald-500 text-emerald-50"
                      : r.pipeline_stage === i
                      ? "bg-amber-400 text-amber-950"
                      : "bg-muted text-muted-foreground",
                  )}
                >
                  {i + 1}
                </div>
              ))}
            </div>
            <span className="text-muted-foreground">
              Stage {r.pipeline_stage}/{STAGE_LABELS.length}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Red flag chip
// ---------------------------------------------------------------------------

const FLAG_LABELS: Record<RedFlagCode, string> = {
  oos_sharpe_far_below_is: "OOS<<IS",
  underperforms_hodl: "輸HODL",
  too_few_trades: "交易少",
  alpha_is_fee_illusion: "費用幻覺",
  overfit_suspect: "疑似過擬",
  regime_conditional: "Regime限定",
};

function RedFlagChip({ code }: { code: RedFlagCode }) {
  return (
    <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300">
      {FLAG_LABELS[code] ?? code}
    </span>
  );
}

// ---------------------------------------------------------------------------
// GateBadge
// ---------------------------------------------------------------------------

function GateBadge({ pass, fatal }: { pass: boolean | null; fatal: boolean | null }) {
  if (pass === null) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <span className="h-2.5 w-2.5 rounded-full bg-muted" />
        N/A
      </span>
    );
  }
  if (fatal) {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-semibold text-red-600 dark:text-red-400">
        <span className="h-2.5 w-2.5 rounded-full bg-red-600" />
        FATAL
      </span>
    );
  }
  if (pass) {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-600 dark:text-emerald-400">
        <span className="h-2.5 w-2.5 rounded-full bg-emerald-500" />
        GO
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs font-semibold text-red-500 dark:text-red-400">
      <span className="h-2.5 w-2.5 rounded-full bg-red-500" />
      NO-GO
    </span>
  );
}

// ---------------------------------------------------------------------------
// Metric cell
// ---------------------------------------------------------------------------

function MetricCell({ value, formatter }: { value: number | null; formatter: (v: number) => string }) {
  if (value === null) return <span className="text-muted-foreground">—</span>;
  return <span>{formatter(value)}</span>;
}

const fmtSharpe = (v: number) => (v >= 0 ? `+${v.toFixed(2)}` : v.toFixed(2));
const fmtPct = (v: number) => `${(v * 100).toFixed(1)}%`;

// ---------------------------------------------------------------------------
// Sortable header cell
// ---------------------------------------------------------------------------

function SortableTh({
  label, sortKey: key, active, dir, onSort, align = "right",
}: {
  label: string;
  sortKey: SortKey;
  active: boolean;
  dir: SortDir;
  onSort: (k: SortKey) => void;
  align?: "right" | "center";
}) {
  return (
    <th
      className={cn("px-4 py-3 cursor-pointer select-none hover:text-foreground", align === "right" ? "text-right" : "text-center")}
      onClick={() => onSort(key)}
    >
      {label}
      <span className="ml-1 text-[10px]">{active ? (dir === "asc" ? "▲" : "▼") : "↕"}</span>
    </th>
  );
}

// ---------------------------------------------------------------------------
// Compare page
// ---------------------------------------------------------------------------

export default function Compare() {
  const navigate = useNavigate();
  const [interval] = useInterval();
  const [rows, setRows] = useState<StrategyRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeCoin, setActiveCoin] = useState<string>("ALL");
  const [sortKey, setSortKey] = useState<SortKey>("rank");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "pipeline_stage" ? "asc" : "desc");
    }
  }

  useEffect(() => {
    setLoading(true);
    api
      .strategies(interval)
      .then((data) => {
        setRows(data);
        setLoading(false);
      })
      .catch((e: Error) => {
        setError(e.message);
        setLoading(false);
      });
  }, [interval]);

  const symbols = ["ALL", ...Array.from(new Set(rows.map((r) => r.symbol))).sort()];
  const filtered = useMemo(() => {
    const base = activeCoin === "ALL" ? rows : rows.filter((r) => r.symbol === activeCoin);
    return [...base].sort(compareBy(sortKey, sortDir));
  }, [rows, activeCoin, sortKey, sortDir]);

  if (loading) {
    return (
      <div className="p-6 text-sm text-muted-foreground animate-pulse">載入策略清單…</div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <div className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-400">
          無法載入策略：{error}
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">策略排名</h1>
        <span className="text-sm text-muted-foreground">{filtered.length} 個策略</span>
      </div>

      {/* Coin filter */}
      <div className="flex gap-2 flex-wrap">
        {symbols.map((sym) => (
          <button
            key={sym}
            onClick={() => setActiveCoin(sym)}
            className={cn(
              "rounded-full px-3 py-1 text-sm font-medium transition-colors",
              activeCoin === sym
                ? "bg-primary text-primary-foreground"
                : "bg-muted text-muted-foreground hover:bg-muted/80",
            )}
          >
            {sym}
          </button>
        ))}
      </div>

      {/* Pipeline strip */}
      <PipelineStrip rows={filtered} />

      {/* Comparison table */}
      {filtered.length === 0 ? (
        <div className="rounded-lg border p-8 text-center text-sm text-muted-foreground">
          {interval === "all" ? "尚無任何策略" : `尚無 ${interval} 策略 — 該時間級別的 pipeline 可能還沒跑或進行中`}
        </div>
      ) : (
        <div className="rounded-lg border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50 text-xs text-muted-foreground uppercase tracking-wide">
                <th className="px-4 py-3 text-left">策略</th>
                <th className="px-4 py-3 text-left">幣種</th>
                <th className="px-4 py-3 text-left">級別</th>
                <SortableTh label="OOS Sharpe" sortKey="sharpe_oos" active={sortKey === "sharpe_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="OOS DD" sortKey="dd_oos" active={sortKey === "dd_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="OOS Trades" sortKey="trades_oos" active={sortKey === "trades_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="IS Sharpe" sortKey="sharpe" active={sortKey === "sharpe"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="Stage" sortKey="pipeline_stage" active={sortKey === "pipeline_stage"} dir={sortDir} onSort={toggleSort} align="center" />
                <th className="px-4 py-3 text-center">GO/NO-GO</th>
                <th className="px-4 py-3 text-left">紅旗</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {filtered.map((row) => (
                <tr
                  key={`${row.interval}_${row.strategy_id}`}
                  onClick={() => navigate(`/strategies/${row.strategy_id}?interval=${row.interval}`)}
                  className={cn(
                    "cursor-pointer transition-colors hover:bg-muted/40",
                    row.gate_fatal && "bg-red-50/50 dark:bg-red-950/20",
                    !row.gate_fatal && row.gate_pass === false && "bg-orange-50/40 dark:bg-orange-950/10",
                  )}
                >
                  <td className="px-4 py-3 font-mono font-medium text-foreground">{row.strategy_id}</td>
                  <td className="px-4 py-3 text-muted-foreground">{row.symbol}</td>
                  <td className="px-4 py-3">
                    <span className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300">
                      {row.interval}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums font-medium">
                    <MetricCell value={row.sharpe_oos} formatter={fmtSharpe} />
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    <MetricCell value={row.dd_oos} formatter={fmtPct} />
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    <MetricCell value={row.trades_oos} formatter={(v) => String(Math.round(v))} />
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-muted-foreground">
                    <MetricCell value={row.sharpe} formatter={fmtSharpe} />
                  </td>
                  <td className="px-4 py-3 text-center text-muted-foreground">{row.pipeline_stage}</td>
                  <td className="px-4 py-3 text-center">
                    <GateBadge pass={row.gate_pass} fatal={row.gate_fatal} />
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {row.red_flags.map((f) => (
                        <RedFlagChip key={f} code={f} />
                      ))}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
