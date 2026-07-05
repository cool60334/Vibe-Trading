"""Trader manager — control-file-driven supervisor for the standalone trader.

Runs as the entrypoint of the independent ``trader`` container (decoupled from
the dashboard server). It watches ``runs/testnet/<id>/control.json`` on the
shared volume and keeps a ``trader.loop`` subprocess running for every control
whose ``desired_state`` is ``running``; it stops the loop when the state flips
to ``stopped`` or the control file disappears.

Because the dashboard server only *writes* control files (never spawns), the
trader lifecycle is independent of dashboard deploys/restarts. On its own
restart the manager re-reads the control files and resumes every running id.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal as _signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("trader.manager")


# ── Pure helpers (shared spawn primitives) ────────────────────────────────────

def require_credentials(env: dict, mode: str) -> None:
    """Raise if real-order modes lack Bybit keys. ``paper`` needs none."""
    if mode in ("testnet", "live"):
        for key in ("BYBIT_API_KEY", "BYBIT_API_SECRET"):
            if not env.get(key):
                raise EnvironmentError(
                    f"{key} not set — cannot start trader in {mode} mode"
                )


def build_trader_command(
    python_exe: str,
    *,
    mode: str,
    strategy_id: str,
    testnet_id: str,
    run_dir: str,
    symbol: str,
    interval: str,
    repo_root: str,
    qty: float,
    lookback: "Optional[int | str]" = None,
) -> list:
    """Build the ``python -m trader.loop`` argv, including ``--mode``.

    ``lookback`` (optional, from control.json) overrides the loop's default
    OHLCV fetch window; the loop still raises it to the strategy's YAML-derived
    rolling-window requirement if that is larger.
    """
    cmd = [
        python_exe, "-m", "trader.loop",
        "--strategy-id", strategy_id,
        "--testnet-id", testnet_id,
        "--run-dir", run_dir,
        "--symbol", symbol,
        "--interval", interval,
        "--repo-root", repo_root,
        "--qty", str(qty),
        "--mode", mode,
    ]
    if lookback is not None:
        cmd += ["--lookback", str(int(lookback))]
    return cmd


def read_control(path: Path) -> Optional[dict]:
    """Parse a control.json, or None if missing/unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_subprocess_env(base_env: dict, ctrl: dict) -> dict:
    """Merge a control file's optional per-strategy ``env`` over the container env.

    The manager passes one shared container environment to every loop it spawns,
    but risk/freshness knobs are per-strategy: a freshness-sensitive strategy on
    an hourly cron needs a tight ``FACTOR_MAX_AGE_DAYS`` while a daily-factor
    strategy in the same container needs the loose default. Likewise
    ``REGIME_MAX_AGE_DAYS`` (regime-staleness warning threshold) can be
    tightened per-strategy for one on a tighter regime-refresh cadence.
    ``control.json`` may
    carry an ``env`` dict whose entries override the base for that strategy only;
    values are coerced to str (env vars are strings). Strategies without an
    ``env`` block keep the container defaults unchanged.
    """
    out = dict(base_env)
    for k, v in (ctrl.get("env") or {}).items():
        out[str(k)] = str(v)
    return out


# ── Manager ───────────────────────────────────────────────────────────────────

class Manager:
    """Reconcile running ``trader.loop`` subprocesses with control files.

    Spawning is injectable (``spawner``/``is_alive``/``stopper``) so the
    reconciliation logic can be unit-tested without real processes.
    """

    def __init__(
        self,
        repo_root,
        dashboard_dir=None,
        *,
        spawner: Optional[Callable[[str, dict], object]] = None,
        is_alive: Optional[Callable[[object], bool]] = None,
        stopper: Optional[Callable[[object], None]] = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.dashboard_dir = (
            Path(dashboard_dir) if dashboard_dir
            else Path(__file__).resolve().parent.parent
        )
        self._procs: dict = {}  # testnet_id -> handle
        self._spawner = spawner or self._default_spawn
        self._is_alive = is_alive or self._default_alive
        self._stopper = stopper or self._default_stop

    # ── Reconciliation ────────────────────────────────────────────────────────

    def _read_controls(self) -> dict:
        base = self.repo_root / "runs" / "testnet"
        controls: dict = {}
        if base.is_dir():
            for p in sorted(base.glob("*/control.json")):
                ctrl = read_control(p)
                if ctrl is not None:
                    controls[p.parent.name] = ctrl
        return controls

    def _alive(self, testnet_id: str) -> bool:
        h = self._procs.get(testnet_id)
        if h is None:
            return False
        if not self._is_alive(h):
            del self._procs[testnet_id]
            return False
        return True

    def _stop(self, testnet_id: str) -> None:
        h = self._procs.pop(testnet_id, None)
        if h is not None:
            logger.info("Stopping trader %s", testnet_id)
            self._stopper(h)

    def scan_once(self) -> None:
        controls = self._read_controls()

        # Stop loops whose control is gone or no longer wants running.
        for testnet_id in list(self._procs):
            ctrl = controls.get(testnet_id)
            if ctrl is None or ctrl.get("desired_state") != "running":
                self._stop(testnet_id)

        # Start loops that should be running but are not.
        for testnet_id, ctrl in controls.items():
            if ctrl.get("desired_state") == "running" and not self._alive(testnet_id):
                try:
                    handle = self._spawner(testnet_id, ctrl)
                except Exception as e:  # don't let one bad control kill the loop
                    logger.error("Spawn failed for %s: %s", testnet_id, e)
                    continue
                if handle is not None:
                    self._procs[testnet_id] = handle
                    logger.info("Started trader %s (mode=%s)", testnet_id, ctrl.get("mode"))

    # ── Default real spawning ─────────────────────────────────────────────────

    def _default_spawn(self, testnet_id: str, ctrl: dict):
        env = build_subprocess_env(os.environ, ctrl)
        mode = (ctrl.get("mode") or env.get("TRADING_MODE", "paper")).lower()
        require_credentials(env, mode)
        cmd = build_trader_command(
            sys.executable,
            mode=mode,
            strategy_id=ctrl["strategy_id"],
            testnet_id=testnet_id,
            run_dir=ctrl["run_dir"],
            symbol=ctrl["symbol"],
            interval=ctrl.get("interval", "1H"),
            repo_root=str(self.repo_root),
            qty=float(ctrl.get("qty", 0.001)),
            lookback=ctrl.get("lookback"),
        )
        env["PYTHONPATH"] = str(self.dashboard_dir) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(cmd, env=env)

    def _default_alive(self, handle) -> bool:
        return handle.poll() is None

    def _default_stop(self, handle) -> None:
        handle.terminate()
        try:
            handle.wait(timeout=10)
        except subprocess.TimeoutExpired:
            handle.kill()

    # ── Run loop ──────────────────────────────────────────────────────────────

    def run(self, scan_interval: float = 5.0) -> None:
        shutdown = {"flag": False}

        def _handle_sig(signum, frame):
            logger.info("Received signal %s — stopping all traders", signum)
            shutdown["flag"] = True

        _signal.signal(_signal.SIGTERM, _handle_sig)
        _signal.signal(_signal.SIGINT, _handle_sig)

        logger.info("Manager started — watching %s", self.repo_root / "runs" / "testnet")
        while not shutdown["flag"]:
            try:
                self.scan_once()
            except Exception as e:
                logger.error("scan_once error: %s", e, exc_info=True)
            elapsed = 0.0
            while elapsed < scan_interval and not shutdown["flag"]:
                time.sleep(min(1.0, scan_interval - elapsed))
                elapsed += 1.0

        for testnet_id in list(self._procs):
            self._stop(testnet_id)
        logger.info("Manager stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Vibe-Trading trader manager")
    parser.add_argument("--repo-root", default="/repo")
    parser.add_argument("--scan-interval", type=float, default=5.0)
    args = parser.parse_args()
    Manager(args.repo_root).run(scan_interval=args.scan_interval)


if __name__ == "__main__":
    main()
