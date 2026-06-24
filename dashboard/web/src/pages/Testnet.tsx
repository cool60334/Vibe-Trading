import { useEffect, useState, useCallback, useRef } from "react";
import {
  api,
  isAbortError,
  type TestnetStatus,
  type LiveBlock,
  type VsBacktestBlock,
  type KillswitchBlock,
  type TestnetAlert,
  type AlertSeverity,
  type LiveStatus,
  type TradingMode,
  type EquityPoint,
  type TestnetTradeRow,
  type StrategyRow,
} from "../lib/api";
import { EquityChart } from "../components/charts/EquityChart";
import { cn } from "../lib/utils";
import { riskHeadroom } from "../lib/risk";

// ---------------------------------------------------------------------------
// Trading-mode badge
// ---------------------------------------------------------------------------

const MODE_LABEL: Record<TradingMode, string> = {
  paper: "PAPER · 主網模擬",
  testnet: "TESTNET · 沙盒",
  live: "LIVE · 真實下單",
};

const MODE_STYLE: Record<TradingMode, string> = {
  paper: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
  testnet: "bg-muted text-muted-foreground",
  live: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
};

function ModeBadge({ mode }: { mode: TradingMode | null }) {
  if (!mode) return null;
  return (
    <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-semibold", MODE_STYLE[mode])}>
      {MODE_LABEL[mode]}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Trader control (start / stop)
// ---------------------------------------------------------------------------

function TraderControls({
  status,
  onAction,
}: {
  status: TestnetStatus;
  onAction: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showStartForm, setShowStartForm] = useState(false);
  const [runDir, setRunDir] = useState("");
  const [qty, setQty] = useState("0.001");
  const [mode, setMode] = useState<TradingMode>(status.mode ?? "paper");

  const isRunning = status.live.status === "running" || status.live.status === "paused";

  const handleStop = async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await api.traderStop(status.testnet_id, status.strategy_id);
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error((j as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      onAction();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleStart = async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await api.traderStart(status.testnet_id, {
        strategy_id: status.strategy_id,
        run_dir: runDir.trim() || undefined,
        symbol: status.symbol,
        qty: parseFloat(qty) || 0.001,
        mode,
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error((j as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      setShowStartForm(false);
      onAction();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        {isRunning ? (
          <button
            onClick={handleStop}
            disabled={loading}
            className="rounded-md border border-red-300 bg-red-50 dark:bg-red-950/30 px-3 py-1.5 text-xs font-medium text-red-700 dark:text-red-400 hover:bg-red-100 disabled:opacity-50 transition-colors"
          >
            {loading ? "停止中…" : "停止 Trader"}
          </button>
        ) : (
          <button
            onClick={() => setShowStartForm((v) => !v)}
            disabled={loading}
            className="rounded-md border border-emerald-300 bg-emerald-50 dark:bg-emerald-950/30 px-3 py-1.5 text-xs font-medium text-emerald-700 dark:text-emerald-400 hover:bg-emerald-100 disabled:opacity-50 transition-colors"
          >
            啟動 Trader
          </button>
        )}
      </div>

      {showStartForm && (
        <div className="rounded-md border p-3 space-y-2 text-xs">
          <div className="space-y-1">
            <label className="text-muted-foreground">Run 目錄（repo-relative，含 code/signal_engine.py）</label>
            <input
              value={runDir}
              onChange={(e) => setRunDir(e.target.value)}
              placeholder="runs/btc_s1_funding_carry_oos"
              className="w-full rounded border px-2 py-1 text-xs bg-background"
            />
          </div>
          <div className="flex gap-3">
            <div className="space-y-1">
              <label className="text-muted-foreground">下單數量（base asset）</label>
              <input
                value={qty}
                onChange={(e) => setQty(e.target.value)}
                placeholder="0.001"
                className="w-32 rounded border px-2 py-1 text-xs bg-background"
              />
            </div>
            <div className="space-y-1">
              <label className="text-muted-foreground">模式</label>
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as TradingMode)}
                className="rounded border px-2 py-1 text-xs bg-background"
              >
                <option value="paper">paper（主網模擬）</option>
                <option value="testnet">testnet（沙盒）</option>
                <option value="live">live（真實下單）</option>
              </select>
            </div>
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleStart}
              disabled={loading || !runDir.trim()}
              className="rounded-md bg-emerald-600 px-3 py-1 text-xs text-white hover:bg-emerald-700 disabled:opacity-50 transition-colors"
            >
              {loading ? "啟動中…" : "確認啟動"}
            </button>
            <button
              onClick={() => setShowStartForm(false)}
              className="rounded-md border px-3 py-1 text-xs hover:bg-muted transition-colors"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {err && (
        <div className="rounded-md border border-red-300 bg-red-50 dark:bg-red-950/30 px-2 py-1 text-xs text-red-700 dark:text-red-400">
          {err}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const fmtPct = (v: number | null) =>
  v === null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
const fmtRatio = (v: number | null) =>
  v === null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(3)}`;
const fmtCurrency = (v: number | null) =>
  v === null ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: 2 });

const STATUS_STYLE: Record<LiveStatus, string> = {
  running: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  paused:  "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
  stopped: "bg-muted text-muted-foreground",
};

const STATUS_DOT: Record<LiveStatus, string> = {
  running: "bg-emerald-500 animate-pulse",
  paused:  "bg-amber-400",
  stopped: "bg-gray-400",
};

const ALERT_STYLE: Record<AlertSeverity, string> = {
  info:     "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-300",
  warning:  "border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-300",
  critical: "border-red-400 bg-red-50 text-red-700 dark:border-red-700 dark:bg-red-950/30 dark:text-red-300",
};

const ALERT_ICON: Record<AlertSeverity, string> = {
  info: "ℹ",
  warning: "⚠",
  critical: "🔴",
};

// ---------------------------------------------------------------------------
// Stat card
// ---------------------------------------------------------------------------

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border p-3 text-center">
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      {sub && <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live metrics row
// ---------------------------------------------------------------------------

function LiveMetrics({ live }: { live: LiveBlock }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatCard label="淨值" value={fmtCurrency(live.equity)} />
      <StatCard label="Sharpe (live)" value={fmtRatio(live.sharpe)} />
      <StatCard label="Max DD" value={fmtPct(live.max_drawdown)} />
      <StatCard label="成交筆數" value={String(live.trades)} sub={`持倉 ${live.open_positions}`} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// vs Backtest panel
// ---------------------------------------------------------------------------

function VsBacktestPanel({ vs }: { vs: VsBacktestBlock }) {
  const sharpeOk =
    vs.sharpe_ratio !== null ? vs.sharpe_ratio >= 0.8 : null;

  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-xs text-muted-foreground">
              <th className="py-2 pr-4 text-left">指標</th>
              <th className="py-2 pr-4 text-right">Live</th>
              <th className="py-2 pr-4 text-right">Backtest</th>
              <th className="py-2 text-right">Live / BT 比</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            <tr>
              <td className="py-2 pr-4 text-muted-foreground">Sharpe</td>
              <td className="py-2 pr-4 text-right tabular-nums">{fmtRatio(vs.live_sharpe)}</td>
              <td className="py-2 pr-4 text-right tabular-nums">{fmtRatio(vs.backtest_sharpe)}</td>
              <td className={cn(
                "py-2 text-right tabular-nums font-medium",
                sharpeOk === true ? "text-emerald-600 dark:text-emerald-400"
                : sharpeOk === false ? "text-red-500 dark:text-red-400"
                : "",
              )}>
                {vs.sharpe_ratio !== null ? vs.sharpe_ratio.toFixed(2) : "—"}
              </td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-muted-foreground">Slippage</td>
              <td className="py-2 pr-4 text-right tabular-nums">{fmtPct(vs.live_slippage)}</td>
              <td className="py-2 pr-4 text-right tabular-nums">{fmtPct(vs.assumed_slippage)}</td>
              <td className="py-2 text-right tabular-nums text-muted-foreground">—</td>
            </tr>
            <tr>
              <td className="py-2 pr-4 text-muted-foreground">未成交訂單</td>
              <td className="py-2 pr-4 text-right tabular-nums" colSpan={3}>{vs.unfilled_orders}</td>
            </tr>
          </tbody>
        </table>
      </div>
      {sharpeOk === false && (
        <div className="rounded-md border border-amber-300 bg-amber-50 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
          ⚠ Live Sharpe 顯著低於 backtest（比值 &lt; 0.8）— 策略可能在實盤表現降級
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Kill switch panel
// ---------------------------------------------------------------------------

function RiskGauge({
  currentDd, pauseDd, terminateDd, triggered,
}: {
  currentDd: number | null;
  pauseDd: number;
  terminateDd: number;
  triggered: boolean;
}) {
  if (currentDd === null) {
    return <p className="text-xs text-muted-foreground">風險餘裕：等待 equity 資料</p>;
  }
  const h = riskHeadroom(currentDd, pauseDd, terminateDd);
  const pausePos = terminateDd > 0 ? Math.min(100, (pauseDd / terminateDd) * 100) : 0;
  const markerPos = h.fillFraction * 100;
  const over = h.toTerminate <= 0;
  const zoneColor =
    triggered || h.zone === "danger" ? "bg-red-500"
    : h.zone === "caution" ? "bg-amber-500"
    : "bg-emerald-500";
  const fmt = (v: number) => `${(v * 100).toFixed(2)}%`;

  return (
    <div className="space-y-1.5">
      <div className="relative h-3 rounded-full overflow-hidden bg-muted">
        {/* safe zone (green) up to pause */}
        <div
          className="absolute inset-y-0 left-0 bg-emerald-200 dark:bg-emerald-900/40"
          style={{ width: `${pausePos}%` }}
        />
        {/* caution zone (amber) pause..terminate */}
        <div
          className="absolute inset-y-0 bg-amber-200 dark:bg-amber-900/40"
          style={{ left: `${pausePos}%`, right: 0 }}
        />
        {/* current DD marker */}
        <div
          className={cn("absolute inset-y-0 w-1.5 rounded", zoneColor)}
          style={{ left: `${markerPos}%`, transform: "translateX(-50%)" }}
        />
        {/* pause threshold line — painted last so it is never buried under a fill */}
        <div className="absolute inset-y-0 w-px bg-amber-600 dark:bg-amber-400" style={{ left: `${pausePos}%` }} />
      </div>
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-muted-foreground">
          當前 DD <span className="font-semibold text-foreground tabular-nums">{fmt(currentDd)}</span>
          {" / 終止 "}
          <span className="tabular-nums">{fmt(terminateDd)}</span>
        </span>
        <span className={cn("tabular-nums", over ? "text-red-600 font-semibold dark:text-red-400" : "text-muted-foreground")}>
          {over
            ? `已逾終止 ${fmt(-h.toTerminate)}`
            : `距暫停 ${fmt(Math.max(0, h.toPause))} · 距終止 ${fmt(h.toTerminate)}`}
        </span>
      </div>
    </div>
  );
}

function KillSwitchPanel({ ks, currentDd }: { ks: KillswitchBlock; currentDd: number | null }) {
  return (
    <div className="space-y-3">
      <RiskGauge
        currentDd={currentDd}
        pauseDd={ks.pause_drawdown}
        terminateDd={ks.terminate_drawdown}
        triggered={ks.triggered}
      />
      {ks.triggered && (
        <div className="rounded-lg border border-red-400 bg-red-50 dark:bg-red-950/30 dark:border-red-700 px-4 py-3">
          <div className="text-sm font-semibold text-red-700 dark:text-red-400">
            🔴 Kill Switch 已觸發
          </div>
          {ks.reason && (
            <div className="text-xs text-red-600 dark:text-red-400 mt-1">
              原因：{ks.reason}
            </div>
          )}
          {ks.triggered_at && (
            <div className="text-xs text-muted-foreground mt-0.5">
              時間：{new Date(ks.triggered_at).toLocaleString("zh-TW")}
            </div>
          )}
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div className="rounded-md border p-3">
          <div className="text-xs text-muted-foreground mb-1">暫停閾值 (DD)</div>
          <div className="font-semibold text-amber-600">{fmtPct(ks.pause_drawdown)}</div>
        </div>
        <div className="rounded-md border p-3">
          <div className="text-xs text-muted-foreground mb-1">終止閾值 (DD)</div>
          <div className="font-semibold text-red-600">{fmtPct(ks.terminate_drawdown)}</div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Alerts list
// ---------------------------------------------------------------------------

function AlertsList({ alerts }: { alerts: TestnetAlert[] }) {
  if (alerts.length === 0) {
    return <p className="text-sm text-muted-foreground">無警報</p>;
  }
  const sorted = [...alerts].sort((a, b) => {
    const order = { critical: 0, warning: 1, info: 2 };
    return order[a.severity] - order[b.severity];
  });
  return (
    <ul className="space-y-2">
      {sorted.map((a, i) => (
        <li
          key={i}
          className={cn("rounded-md border px-3 py-2 text-sm flex items-start gap-2", ALERT_STYLE[a.severity])}
        >
          <span className="mt-0.5">{ALERT_ICON[a.severity]}</span>
          <div className="flex-1">
            <span>{a.message}</span>
            <span className="ml-2 text-xs opacity-60">
              {new Date(a.timestamp).toLocaleString("zh-TW")}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Live equity curve + trades (paper/testnet/live trader output)
// ---------------------------------------------------------------------------

function toEquityPoints(rows: { timestamp: string; equity: number }[]): EquityPoint[] {
  let peak = -Infinity;
  return rows.map((r) => {
    const eq = Number(r.equity);
    peak = Math.max(peak, eq);
    const drawdown = peak > 0 ? (eq - peak) / peak : 0;
    return { time: r.timestamp, equity: eq, drawdown };
  });
}

function LiveEquity({ testnetId, refreshKey }: { testnetId: string; refreshKey: number }) {
  const [points, setPoints] = useState<EquityPoint[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .testnetEquity(testnetId)
      .then((rows) => { if (alive) { setPoints(toEquityPoints(rows)); setErr(null); } })
      .catch((e: Error) => { if (alive) setErr(e.message); });
    return () => { alive = false; };
  }, [testnetId, refreshKey]);

  if (err) return <p className="text-xs text-muted-foreground">淨值曲線尚無資料（trader 還沒寫第一筆）</p>;
  if (points.length === 0) return <p className="text-xs text-muted-foreground">淨值曲線載入中…</p>;
  return <EquityChart data={points} height={220} />;
}

function LiveTrades({ testnetId, refreshKey }: { testnetId: string; refreshKey: number }) {
  const [trades, setTrades] = useState<TestnetTradeRow[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .testnetTrades(testnetId)
      .then((rows) => { if (alive) { setTrades(rows); setErr(null); } })
      .catch((e: Error) => { if (alive) setErr(e.message); });
    return () => { alive = false; };
  }, [testnetId, refreshKey]);

  if (err || trades.length === 0) return <p className="text-sm text-muted-foreground">尚無成交</p>;

  const recent = [...trades].reverse().slice(0, 30);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b text-muted-foreground">
            <th className="py-1.5 pr-3 text-left">時間</th>
            <th className="py-1.5 pr-3 text-left">方向</th>
            <th className="py-1.5 pr-3 text-right">數量</th>
            <th className="py-1.5 text-right">價格</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {recent.map((t, i) => (
            <tr key={i}>
              <td className="py-1.5 pr-3 tabular-nums">{new Date(t.timestamp).toLocaleString("zh-TW")}</td>
              <td className={cn("py-1.5 pr-3 font-medium", t.side === "buy" ? "text-emerald-600 dark:text-emerald-400" : "text-red-500 dark:text-red-400")}>
                {t.side === "buy" ? "買 / 多" : "賣 / 空"}
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{t.qty}</td>
              <td className="py-1.5 text-right tabular-nums">{Number(t.price).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single testnet card
// ---------------------------------------------------------------------------

function TestnetCard({
  status,
  onRefresh,
  refreshKey,
}: {
  status: TestnetStatus;
  onRefresh: () => void;
  refreshKey: number;
}) {
  const { live, vs_backtest, killswitch, alerts } = status;
  const updatedAgo = Math.round(
    (Date.now() - new Date(live.updated_at).getTime()) / 1000
  );

  return (
    <div className="rounded-xl border shadow-sm space-y-0">
      {/* Card header */}
      <div className="flex items-center gap-3 px-5 py-4 border-b">
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold",
            STATUS_STYLE[live.status],
          )}
        >
          <span className={cn("h-2 w-2 rounded-full", STATUS_DOT[live.status])} />
          {live.status.toUpperCase()}
        </span>
        <span className="font-mono font-semibold">{status.strategy_id}</span>
        <span className="text-sm text-muted-foreground">{status.symbol}</span>
        <ModeBadge mode={status.mode} />
        <span className="ml-auto text-xs text-muted-foreground">
          更新於 {updatedAgo}s 前
        </span>
      </div>

      <div className="p-5 space-y-5">
        {/* Kill switch alert (if triggered, show at top) */}
        {killswitch.triggered && (
          <div className="rounded-lg border border-red-400 bg-red-50 dark:bg-red-950/30 dark:border-red-700 px-4 py-3">
            <div className="text-sm font-semibold text-red-700 dark:text-red-400">
              🔴 Kill Switch 已觸發 — {killswitch.reason ?? "超過回撤閾值"}
            </div>
          </div>
        )}

        {/* Live metrics */}
        <LiveMetrics live={live} />

        {/* Live equity curve */}
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-2">即時淨值曲線</div>
          <LiveEquity testnetId={status.testnet_id} refreshKey={refreshKey} />
        </div>

        {/* Live trades */}
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-2">成交明細（最近 30 筆）</div>
          <LiveTrades testnetId={status.testnet_id} refreshKey={refreshKey} />
        </div>

        {/* vs Backtest */}
        {vs_backtest ? (
          <div>
            <div className="text-xs font-medium text-muted-foreground mb-2">Live vs Backtest</div>
            <VsBacktestPanel vs={vs_backtest} />
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">vs Backtest 資料尚未產生</p>
        )}

        {/* Kill switch thresholds */}
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-2">Kill Switch</div>
          <KillSwitchPanel ks={killswitch} currentDd={live.max_drawdown} />
        </div>

        {/* Alerts */}
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-2">
            警報 {alerts.length > 0 && `(${alerts.length})`}
          </div>
          <AlertsList alerts={alerts} />
        </div>

        {/* Trader controls */}
        <div>
          <div className="text-xs font-medium text-muted-foreground mb-2">Trader 控制</div>
          <TraderControls status={status} onAction={onRefresh} />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Paper-run launcher — start the FIRST run for a strategy (no card needed yet)
// ---------------------------------------------------------------------------

function PaperRunLauncher({ onLaunched }: { onLaunched: () => void }) {
  const [open, setOpen] = useState(false);
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [strategyId, setStrategyId] = useState("");
  const [runDir, setRunDir] = useState("");
  const [symbol, setSymbol] = useState("");
  const [qty, setQty] = useState("0.001");
  const [mode, setMode] = useState<TradingMode>("paper");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    api.strategies().then(setStrategies).catch(() => {});
  }, [open]);

  const onSelectStrategy = (id: string) => {
    setStrategyId(id);
    const row = strategies.find((s) => s.strategy_id === id);
    if (row) setSymbol(row.symbol.includes("/") ? row.symbol : `${row.symbol}/USDT:USDT`);
  };

  const launch = async () => {
    setLoading(true);
    setErr(null);
    try {
      const testnetId = `${strategyId}_${mode}`;
      const res = await api.traderStart(testnetId, {
        strategy_id: strategyId,
        run_dir: runDir.trim() || undefined,
        symbol: symbol.trim() || undefined,
        qty: parseFloat(qty) || 0.001,
        mode,
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error((j as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      setOpen(false);
      onLaunched();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="rounded-xl border border-dashed p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">啟動新的 dry-run（paper）</div>
        <button
          onClick={() => setOpen((v) => !v)}
          className="rounded-md border px-3 py-1 text-xs hover:bg-muted transition-colors"
        >
          {open ? "收合" : "新增 run"}
        </button>
      </div>
      {open && (
        <div className="space-y-2 text-xs">
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-muted-foreground">策略</label>
              <select
                value={strategyId}
                onChange={(e) => onSelectStrategy(e.target.value)}
                className="w-full rounded border px-2 py-1 bg-background"
              >
                <option value="">— 選擇策略 —</option>
                {strategies.map((s) => (
                  <option key={s.strategy_id} value={s.strategy_id}>
                    {s.strategy_id}（{s.symbol}）
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <label className="text-muted-foreground">模式</label>
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as TradingMode)}
                className="w-full rounded border px-2 py-1 bg-background"
              >
                <option value="paper">paper（主網模擬）</option>
                <option value="testnet">testnet（沙盒）</option>
                <option value="live">live（真實下單）</option>
              </select>
            </div>
          </div>
          <div className="space-y-1">
            <label className="text-muted-foreground">Run 目錄（repo-relative，含 code/signal_engine.py）</label>
            <input
              value={runDir}
              onChange={(e) => setRunDir(e.target.value)}
              placeholder="runs/eth_s5_oos"
              className="w-full rounded border px-2 py-1 bg-background"
            />
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-muted-foreground">Symbol（ccxt）</label>
              <input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="BTC/USDT:USDT"
                className="w-full rounded border px-2 py-1 bg-background"
              />
            </div>
            <div className="space-y-1">
              <label className="text-muted-foreground">下單數量（base asset）</label>
              <input
                value={qty}
                onChange={(e) => setQty(e.target.value)}
                placeholder="0.001"
                className="w-full rounded border px-2 py-1 bg-background"
              />
            </div>
          </div>
          <div className="flex gap-2">
            <button
              onClick={launch}
              disabled={loading || !strategyId || !runDir.trim()}
              className="rounded-md bg-blue-600 px-3 py-1 text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {loading ? "啟動中…" : "啟動"}
            </button>
            <button
              onClick={() => setOpen(false)}
              className="rounded-md border px-3 py-1 hover:bg-muted transition-colors"
            >
              取消
            </button>
          </div>
          {err && (
            <div className="rounded-md border border-red-300 bg-red-50 dark:bg-red-950/30 px-2 py-1 text-red-700 dark:text-red-400">
              {err}
            </div>
          )}
          <p className="text-[11px] text-muted-foreground">
            提示：策略需先 promote 才能啟動；testnet_id 會用 <code>策略_模式</code>。
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Testnet page
// ---------------------------------------------------------------------------

const POLL_INTERVAL_MS = 15_000;

export default function Testnet() {
  const [statuses, setStatuses] = useState<TestnetStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastFetch, setLastFetch] = useState<Date | null>(null);

  // Abort the in-flight poll before starting the next one so a slow response
  // can't resolve after a newer one and overwrite the kill-switch DD gauge with
  // stale data (masking a live drawdown from the operator).
  const ctrlRef = useRef<AbortController | null>(null);
  const fetchData = useCallback(() => {
    ctrlRef.current?.abort();
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    api
      .testnet({ signal: ctrl.signal })
      .then((data) => {
        setStatuses(data);
        setLastFetch(new Date());
        setError(null);
        setLoading(false);
      })
      .catch((e: Error) => {
        if (isAbortError(e)) return;
        setError(e.message);
        setLoading(false);
      });
  }, []);

  // Initial fetch + polling
  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, POLL_INTERVAL_MS);
    return () => {
      clearInterval(timer);
      ctrlRef.current?.abort();
    };
  }, [fetchData]);

  const refreshKey = lastFetch ? lastFetch.getTime() : 0;

  return (
    <div className="p-6 space-y-4 max-w-4xl mx-auto">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">實盤監控（Paper / Testnet / Live）</h1>
        <div className="flex items-center gap-3">
          {lastFetch && (
            <span className="text-xs text-muted-foreground">
              {lastFetch.toLocaleTimeString("zh-TW")} 更新
            </span>
          )}
          <button
            onClick={fetchData}
            className="rounded-md border px-3 py-1.5 text-xs hover:bg-muted transition-colors"
          >
            重新整理
          </button>
        </div>
      </div>

      <PaperRunLauncher onLaunched={fetchData} />

      {loading && (
        <div className="text-sm text-muted-foreground animate-pulse">載入 testnet 狀態…</div>
      )}

      {error && (
        <div className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-400">
          載入失敗：{error}
        </div>
      )}

      {!loading && !error && statuses.length === 0 && (
        /* Waiting state — no promoted strategies yet */
        <div className="rounded-xl border border-dashed p-12 text-center space-y-3">
          <div className="text-4xl">⏳</div>
          <div className="text-base font-medium text-muted-foreground">
            尚無 Testnet 運行資料
          </div>
          <div className="text-sm text-muted-foreground max-w-sm mx-auto">
            先在策略頁面 Promote 策略到 Testnet，
            v1.5 trader 啟動後資料會出現在這裡。
          </div>
          <div className="text-xs text-muted-foreground">
            每 {POLL_INTERVAL_MS / 1000}s 自動重新整理
          </div>
        </div>
      )}

      {statuses.map((s) => (
        <TestnetCard key={s.testnet_id} status={s} onRefresh={fetchData} refreshKey={refreshKey} />
      ))}
    </div>
  );
}
