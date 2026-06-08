import { useEffect, useState } from "react";
import { RefreshCw, ChevronRight, ChevronDown } from "lucide-react";
import { api, type PipelineStatus, type StageStatus } from "../lib/api";
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

export default function PipelineStatus() {
  const [data, setData] = useState<PipelineStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  function load() {
    setLoading(true);
    api
      .getPipelineStatus()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

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
                <span className="ml-auto text-xs text-muted-foreground">
                  {sym.strategies.length} 策略
                </span>
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
    </div>
  );
}
