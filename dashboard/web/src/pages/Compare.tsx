import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type StrategyRow, type RedFlagCode } from "../lib/api";
import { compareBy, type SortKey, type SortDir } from "../lib/ranking";
import {
  statusBucket,
  nextStep,
  BUCKET_ORDER,
  BUCKET_META,
  type StatusBucket,
  type ChipTone,
} from "../lib/status";
import { useInterval } from "@/hooks/useInterval";
import { cn } from "../lib/utils";

// ---------------------------------------------------------------------------
// Shared style maps
// ---------------------------------------------------------------------------

const TONE_CHIP: Record<ChipTone, string> = {
  success: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  warning: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
  neutral: "bg-muted text-muted-foreground",
  danger: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  info: "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300",
};

const BUCKET_ACCENT: Record<StatusBucket, string> = {
  running: "text-sky-600 dark:text-sky-400",
  deployable: "text-emerald-600 dark:text-emerald-400",
  iterate: "text-amber-600 dark:text-amber-400",
  dead: "text-red-600 dark:text-red-400",
  incomplete: "text-muted-foreground",
};

const BUCKET_BORDER: Record<StatusBucket, string> = {
  running: "border-l-sky-500",
  deployable: "border-l-emerald-500",
  iterate: "border-l-amber-400",
  dead: "border-l-red-500",
  incomplete: "border-l-border",
};

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
// Next-step chip
// ---------------------------------------------------------------------------

function NextStepChip({ row }: { row: StrategyRow }) {
  const step = nextStep(row);
  return (
    <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium", TONE_CHIP[step.tone])}>
      {step.label}
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
// Mini stage indicator (replaces the old standalone PipelineStrip)
// ---------------------------------------------------------------------------

const STAGE_LABELS = ["因子", "策略", "回測", "優化", "選擇"] as const;

function StageMini({ stage }: { stage: number }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-flex gap-0.5">
        {STAGE_LABELS.map((label, i) => (
          <span
            key={i}
            title={`Stage ${i + 1}: ${label}`}
            className={cn(
              "h-2.5 w-2.5 rounded-[2px]",
              stage > i ? "bg-emerald-500" : stage === i ? "bg-amber-400" : "bg-muted",
            )}
          />
        ))}
      </span>
      <span className="text-[10px] text-muted-foreground tabular-nums">{stage}/5</span>
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

const COL_COUNT = 8; // 策略 / 幣種 / OOS Sharpe / OOS DD / OOS Trades / Stage / 判定+下一步 / 紅旗

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------

function SummaryCards({
  counts, active, onToggle,
}: {
  counts: Record<StatusBucket, number>;
  active: StatusBucket | null;
  onToggle: (b: StatusBucket) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {BUCKET_ORDER.map((b) => (
        <button
          key={b}
          onClick={() => onToggle(b)}
          className={cn(
            "rounded-lg border border-l-4 bg-card px-3 py-2 text-left transition-colors hover:bg-muted/40",
            BUCKET_BORDER[b],
            active === b && "ring-2 ring-primary",
          )}
        >
          <div className={cn("text-xs font-medium", BUCKET_ACCENT[b])}>{BUCKET_META[b].label}</div>
          <div className="text-xl font-semibold tabular-nums">{counts[b]}</div>
        </button>
      ))}
    </div>
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
  const [bucketFilter, setBucketFilter] = useState<StatusBucket | null>(null);
  const [openGroups, setOpenGroups] = useState<Record<StatusBucket, boolean>>({
    running: BUCKET_META.running.defaultOpen,
    deployable: BUCKET_META.deployable.defaultOpen,
    iterate: BUCKET_META.iterate.defaultOpen,
    dead: BUCKET_META.dead.defaultOpen,
    incomplete: BUCKET_META.incomplete.defaultOpen,
  });
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

  function toggleGroup(b: StatusBucket) {
    setOpenGroups((g) => ({ ...g, [b]: !g[b] }));
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

  // Coin filter first — summary counts reflect the visible coin scope.
  const coinFiltered = useMemo(
    () => (activeCoin === "ALL" ? rows : rows.filter((r) => r.symbol === activeCoin)),
    [rows, activeCoin],
  );

  const counts = useMemo(() => {
    const c: Record<StatusBucket, number> = { running: 0, deployable: 0, iterate: 0, dead: 0, incomplete: 0 };
    for (const r of coinFiltered) c[statusBucket(r)] += 1;
    return c;
  }, [coinFiltered]);

  // Group rows by bucket; sort within each group by the active column.
  const groups = useMemo(() => {
    const cmp = compareBy(sortKey, sortDir);
    return BUCKET_ORDER.map((b) => ({
      bucket: b,
      rows: coinFiltered.filter((r) => statusBucket(r) === b).sort(cmp),
    })).filter((g) => g.rows.length > 0 && (bucketFilter === null || bucketFilter === g.bucket));
  }, [coinFiltered, sortKey, sortDir, bucketFilter]);

  if (loading) {
    return <div className="p-6 text-sm text-muted-foreground animate-pulse">載入策略清單…</div>;
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
        <span className="text-sm text-muted-foreground">{coinFiltered.length} 個策略</span>
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

      {/* Status summary — click a card to filter to that bucket */}
      <SummaryCards
        counts={counts}
        active={bucketFilter}
        onToggle={(b) => setBucketFilter((cur) => (cur === b ? null : b))}
      />

      {/* Grouped comparison table */}
      {coinFiltered.length === 0 ? (
        <div className="rounded-lg border p-8 text-center text-sm text-muted-foreground">
          {interval === "all" ? "尚無任何策略" : `尚無 ${interval} 策略 — 該時間級別的 pipeline 可能還沒跑或進行中`}
        </div>
      ) : groups.length === 0 ? (
        <div className="rounded-lg border p-8 text-center text-sm text-muted-foreground">
          此分類無策略
        </div>
      ) : (
        <div className="rounded-lg border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50 text-xs text-muted-foreground uppercase tracking-wide">
                <th className="px-4 py-3 text-left">策略</th>
                <th className="px-4 py-3 text-left">幣種</th>
                <SortableTh label="OOS Sharpe" sortKey="sharpe_oos" active={sortKey === "sharpe_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="OOS DD" sortKey="dd_oos" active={sortKey === "dd_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="OOS Trades" sortKey="trades_oos" active={sortKey === "trades_oos"} dir={sortDir} onSort={toggleSort} />
                <SortableTh label="Stage" sortKey="pipeline_stage" active={sortKey === "pipeline_stage"} dir={sortDir} onSort={toggleSort} align="center" />
                <th className="px-4 py-3 text-left">判定 / 下一步</th>
                <th className="px-4 py-3 text-left">紅旗</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {groups.map(({ bucket, rows: groupRows }) => {
                const open = openGroups[bucket] || bucketFilter === bucket;
                return (
                  <FragmentGroup
                    key={bucket}
                    bucket={bucket}
                    rows={groupRows}
                    open={open}
                    onToggle={() => toggleGroup(bucket)}
                    onRowClick={(row) => navigate(`/strategies/${row.strategy_id}?interval=${row.interval}`)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// One bucket's section header + its (collapsible) rows
// ---------------------------------------------------------------------------

function FragmentGroup({
  bucket, rows, open, onToggle, onRowClick,
}: {
  bucket: StatusBucket;
  rows: StrategyRow[];
  open: boolean;
  onToggle: () => void;
  onRowClick: (row: StrategyRow) => void;
}) {
  const meta = BUCKET_META[bucket];
  return (
    <>
      <tr className="cursor-pointer bg-muted/30 hover:bg-muted/50" onClick={onToggle}>
        <td colSpan={COL_COUNT} className="px-4 py-2">
          <span className={cn("text-xs font-semibold", BUCKET_ACCENT[bucket])}>
            <span className="inline-block w-3">{open ? "▾" : "▸"}</span>
            {meta.label} · {rows.length}
          </span>
          <span className="ml-2 text-xs font-normal text-muted-foreground">{meta.hint}</span>
        </td>
      </tr>
      {open &&
        rows.map((row) => (
          <tr
            key={`${row.interval}_${row.strategy_id}`}
            onClick={() => onRowClick(row)}
            className={cn(
              "cursor-pointer transition-colors hover:bg-muted/40",
              row.gate_fatal && "bg-red-50/50 dark:bg-red-950/20",
              !row.gate_fatal && row.gate_pass === false && "bg-orange-50/40 dark:bg-orange-950/10",
            )}
          >
            <td className="px-4 py-3 font-mono font-medium text-foreground">{row.strategy_id}</td>
            <td className="px-4 py-3 text-muted-foreground">{row.symbol}</td>
            <td className="px-4 py-3 text-right tabular-nums font-medium">
              <MetricCell value={row.sharpe_oos} formatter={fmtSharpe} />
            </td>
            <td className="px-4 py-3 text-right tabular-nums">
              <MetricCell value={row.dd_oos} formatter={fmtPct} />
            </td>
            <td className="px-4 py-3 text-right tabular-nums">
              <MetricCell value={row.trades_oos} formatter={(v) => String(Math.round(v))} />
            </td>
            <td className="px-4 py-3 text-center">
              <StageMini stage={row.pipeline_stage} />
            </td>
            <td className="px-4 py-3">
              <div className="flex items-center gap-2">
                <GateBadge pass={row.gate_pass} fatal={row.gate_fatal} />
                <NextStepChip row={row} />
              </div>
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
    </>
  );
}
