import { useEffect, useState, useCallback } from "react";
import { RefreshCw, ChevronRight, ChevronDown, Play } from "lucide-react";
import {
  api,
  type PipelineStatus,
  type StageStatus,
  type PipelineJob,
  type PipelineJobDetail,
} from "../lib/api";
import { cn } from "../lib/utils";

function StageChip({ s }: { s: StageStatus }) {
  const color =
    s.state === "done"
      ? "bg-emerald-500 text-emerald-50"
      : s.state === "stale"
      ? "bg-amber-400 text-amber-950"
      : "bg-muted text-muted-foreground";
  const metric = s.metric_value ? `${s.metric_label}: ${s.metric_value}` : "—";
  const when = s.generated_at ? new Date(s.generated_at).toLocaleString() : "not run";
  return (
    <div
      title={`${s.label} · ${s.state}\n${metric}\n${when}`}
      className={cn("flex flex-col items-center justify-center rounded-sm px-2 py-1 min-w-[64px]", color)}
    >
      <span className="text-[10px] font-semibold leading-tight">{s.stage_id}</span>
      <span className="text-[10px] leading-tight truncate max-w-[60px]">
        {s.metric_value ?? "—"}
      </span>
    </div>
  );
}

const RUNNABLE_STAGES = ["0a", "0", "1", "2", "2.5", "3", "4", "5"];

function RunBar({ onStarted, busy }: { onStarted: () => void; busy: boolean }) {
  const [stage, setStage] = useState("0a");
  const [err, setErr] = useState<string | null>(null);

  async function run(kind: "stage" | "pipeline") {
    setErr(null);
    try {
      await api.runPipeline(kind === "stage" ? { kind, stage } : { kind: "pipeline" });
      onStarted();
    } catch (e) {
      setErr(String(e));
    }
  }

  return (
    <div className="flex items-center gap-2 text-xs">
      <button
        onClick={() => run("pipeline")}
        disabled={busy}
        className="flex items-center gap-1 px-2.5 py-1 rounded-md bg-primary text-primary-foreground disabled:opacity-50"
      >
        <Play className="h-3.5 w-3.5" /> Run 全 pipeline
      </button>
      <span className="text-muted-foreground">或</span>
      <select
        value={stage}
        onChange={(e) => setStage(e.target.value)}
        className="border rounded-md px-1.5 py-1 bg-background"
      >
        {RUNNABLE_STAGES.map((s) => (
          <option key={s} value={s}>
            stage {s}
          </option>
        ))}
      </select>
      <button
        onClick={() => run("stage")}
        disabled={busy}
        className="flex items-center gap-1 px-2.5 py-1 rounded-md border disabled:opacity-50"
      >
        <Play className="h-3.5 w-3.5" /> Run
      </button>
      {busy && <span className="text-amber-500">job 執行中…</span>}
      {err && <span className="text-red-500">{err}</span>}
    </div>
  );
}

function JobsPanel({
  jobs,
  selected,
  onSelect,
  onCancel,
}: {
  jobs: PipelineJob[];
  selected: PipelineJobDetail | null;
  onSelect: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  const color = (s: PipelineJob["status"]) =>
    s === "succeeded"
      ? "text-emerald-500"
      : s === "failed"
      ? "text-red-500"
      : s === "running"
      ? "text-amber-500"
      : s === "canceled"
      ? "text-muted-foreground"
      : "text-foreground";
  return (
    <div className="rounded-lg border bg-card p-3 space-y-2">
      <div className="text-sm font-medium">Jobs</div>
      {jobs.length === 0 && <div className="text-xs text-muted-foreground">尚無 job</div>}
      <div className="flex flex-col gap-1">
        {jobs.slice(0, 10).map((j) => (
          <div key={j.job_id} className="flex items-center gap-2 text-xs">
            <button onClick={() => onSelect(j.job_id)} className="font-mono truncate w-52 text-left hover:underline">
              {j.kind === "pipeline" ? "全 pipeline" : `stage ${j.stage}${j.stress ? " +stress" : ""}`} · {j.job_id.slice(-6)}
            </button>
            <span className={cn("w-20", color(j.status))}>{j.status}</span>
            {(j.status === "queued" || j.status === "running") && (
              <button onClick={() => onCancel(j.job_id)} className="text-muted-foreground hover:text-red-500">
                取消
              </button>
            )}
          </div>
        ))}
      </div>
      {selected && (
        <pre className="mt-2 max-h-64 overflow-auto rounded bg-muted p-2 text-[11px] leading-tight whitespace-pre-wrap">
          {selected.log_tail || "(無 log)"}
        </pre>
      )}
    </div>
  );
}

const PER_SYMBOL_STAGES = ["0a", "0", "1", "2", "2.5", "3", "4"]; // no 5 (global)

function SymbolRunControl({
  symbol,
  onStarted,
  busy,
}: {
  symbol: string;
  onStarted: () => void;
  busy: boolean;
}) {
  const [stage, setStage] = useState("0a");
  const [stress, setStress] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
      <select
        value={stage}
        onChange={(e) => { setStage(e.target.value); setStress(false); }}
        className="border rounded px-1 py-0.5 text-[11px] bg-background"
      >
        <option value="__all__">全 pipeline</option>
        {PER_SYMBOL_STAGES.map((s) => (
          <option key={s} value={s}>
            stage {s}
          </option>
        ))}
      </select>
      {stage === "3" && (
        <label className="flex items-center gap-0.5 text-[11px] text-muted-foreground">
          <input
            type="checkbox"
            checked={stress}
            onChange={(e) => setStress(e.target.checked)}
          />
          cost-stress
        </label>
      )}
      <button
        disabled={busy}
        onClick={() => {
          setErr(null);
          api
            .runPipeline(
              stage === "__all__"
                ? { kind: "pipeline", symbol }
                : { kind: "stage", stage, symbol, stress: stage === "3" && stress },
            )
            .then(onStarted)
            .catch((e: unknown) => setErr(String(e)));
        }}
        className="flex items-center gap-0.5 px-1.5 py-0.5 rounded border text-[11px] disabled:opacity-50"
      >
        <Play className="h-3 w-3" /> Run
      </button>
      {err && <span className="text-[10px] text-destructive">{err}</span>}
    </div>
  );
}

export default function PipelineStatus() {
  const [data, setData] = useState<PipelineStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = useCallback(() => {
    setLoading(true);
    api
      .getPipelineStatus()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const [jobs, setJobs] = useState<PipelineJob[]>([]);
  const [selectedJob, setSelectedJob] = useState<PipelineJobDetail | null>(null);

  const refreshJobs = useCallback(() => {
    api.listPipelineJobs().then(setJobs).catch(() => {});
  }, []);

  const busy = jobs.some((j) => j.status === "queued" || j.status === "running");

  useEffect(() => {
    refreshJobs();
  }, [refreshJobs]);

  // Poll while any job is active; on transition to idle, refresh the status grid.
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => {
      api.listPipelineJobs().then((js) => {
        const wasActive = js.some((j) => j.status === "queued" || j.status === "running");
        setJobs(js);
        if (selectedJob) {
          api.getPipelineJob(selectedJob.job_id).then(setSelectedJob).catch(() => {});
        }
        if (!wasActive) load();
      });
    }, 3000);
    return () => clearInterval(t);
  }, [busy, selectedJob, load]);

  function selectJob(id: string) {
    api.getPipelineJob(id).then(setSelectedJob).catch(() => {});
  }
  function cancelJob(id: string) {
    api.cancelPipelineJob(id).then(refreshJobs).catch(() => {});
  }

  if (loading && !data) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>;
  if (error) return <div className="p-6 text-sm text-red-500">Error: {error}</div>;
  if (!data) return null;

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Pipeline 狀態</h1>
        <button
          onClick={load}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs border hover:bg-muted"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          Refresh
        </button>
        <RunBar onStarted={refreshJobs} busy={busy} />
        <span className="text-xs text-muted-foreground ml-auto">
          掃描於 {new Date(data.generated_at).toLocaleString()}
        </span>
      </div>

      <div className="space-y-3">
        {data.symbols.map((sym) => {
          const open = expanded[sym.symbol] ?? false;
          return (
            <div key={sym.symbol} className="rounded-lg border bg-card">
              <button
                onClick={() => setExpanded((e) => ({ ...e, [sym.symbol]: !open }))}
                className="w-full flex items-center gap-3 p-3 text-left"
              >
                {sym.strategies.length > 0 ? (
                  open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />
                ) : (
                  <span className="w-4" />
                )}
                <span className="font-mono font-semibold uppercase w-16">{sym.symbol}</span>
                <div className="flex gap-1 flex-wrap">
                  {sym.stages.map((s) => (
                    <StageChip key={s.stage_id} s={s} />
                  ))}
                </div>
                <div className="ml-auto flex items-center gap-3">
                  <SymbolRunControl symbol={sym.symbol} onStarted={refreshJobs} busy={busy} />
                  <span className="text-xs text-muted-foreground">
                    {sym.strategies.length} 策略
                  </span>
                </div>
              </button>

              {open && sym.strategies.length > 0 && (
                <div className="border-t px-3 py-2 space-y-1.5">
                  {sym.strategies.map((st) => (
                    <div key={st.strategy_id} className="flex items-center gap-3">
                      <a
                        href={`/strategies/${st.strategy_id}`}
                        className="font-mono text-xs w-56 truncate text-primary hover:underline"
                      >
                        {st.strategy_id}
                      </a>
                      <div className="flex gap-1 flex-wrap">
                        {st.stages.map((s) => (
                          <StageChip key={s.stage_id} s={s} />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <JobsPanel jobs={jobs} selected={selectedJob} onSelect={selectJob} onCancel={cancelJob} />
    </div>
  );
}
