"""Trader control writer — the dashboard server's handle on the decoupled trader.

The server no longer spawns trader subprocesses. Instead it writes a control
file (``runs/testnet/<id>/control.json``) on the shared volume; the standalone
trader container's Manager (``trader.manager``) reconciles ``trader.loop``
processes to those files. This keeps the trader lifecycle independent of
dashboard restarts and deploys.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def require_credentials(env: dict, mode: str) -> None:
    """Raise if real-order modes lack Bybit keys. ``paper`` needs none.

    Server-side pre-check so the UI gets an immediate error; the trader
    Manager validates again authoritatively before spawning.
    """
    if mode in ("testnet", "live"):
        for key in ("BYBIT_API_KEY", "BYBIT_API_SECRET"):
            if not env.get(key):
                raise EnvironmentError(
                    f"{key} not set — cannot start trader in {mode} mode"
                )


class Supervisor:
    """Writes per-strategy control files that the trader Manager reconciles."""

    def __init__(self, repo_root: Path, dashboard_dir: Optional[Path] = None) -> None:
        self.repo_root = Path(repo_root)
        self.dashboard_dir = Path(dashboard_dir) if dashboard_dir else None

    # ── Control file ──────────────────────────────────────────────────────────

    def _control_path(self, testnet_id: str) -> Path:
        return self.repo_root / "runs" / "testnet" / testnet_id / "control.json"

    def _read_control(self, testnet_id: str) -> Optional[dict]:
        path = self._control_path(testnet_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_control(self, testnet_id: str, ctrl: dict) -> None:
        path = self._control_path(testnet_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ctrl, indent=2), encoding="utf-8")

    # ── Public API (signatures kept for the API layer) ────────────────────────

    def start(
        self,
        strategy_id: str,
        testnet_id: str,
        run_dir: str,
        symbol: str,
        interval: str = "1H",
        qty: float = 0.001,
        mode: Optional[str] = None,
    ) -> None:
        """Request a running trader for *testnet_id* by writing its control file.

        Bybit keys are required only for testnet/live. Does not spawn anything —
        the trader Manager picks the control file up.
        """
        mode = (mode or os.environ.get("TRADING_MODE", "paper")).lower()
        require_credentials(os.environ, mode)
        self._write_control(testnet_id, {
            "desired_state": "running",
            "strategy_id": strategy_id,
            "run_dir": run_dir,
            "symbol": symbol,
            "interval": interval,
            "qty": qty,
            "mode": mode,
            "updated_at": _now_iso(),
        })
        logger.info("control=running for %s (mode=%s)", testnet_id, mode)

    def stop(self, testnet_id: str) -> bool:
        """Flip the control file to stopped. Returns False if no control exists."""
        ctrl = self._read_control(testnet_id)
        if ctrl is None:
            return False
        ctrl["desired_state"] = "stopped"
        ctrl["updated_at"] = _now_iso()
        self._write_control(testnet_id, ctrl)
        logger.info("control=stopped for %s", testnet_id)
        return True

    def is_running(self, testnet_id: str) -> bool:
        ctrl = self._read_control(testnet_id)
        return bool(ctrl and ctrl.get("desired_state") == "running")

    def status(self, testnet_id: str) -> dict:
        ctrl = self._read_control(testnet_id)
        desired = ctrl.get("desired_state") if ctrl else None
        return {
            "running": desired == "running",
            "desired_state": desired,
            "testnet_id": testnet_id,
        }

    def stop_all(self) -> None:
        """No-op: the trader runs independently; server shutdown must not stop it."""
        return


# Module-level singleton — populated by main.py on startup
_supervisor: Optional[Supervisor] = None


def get_supervisor() -> Supervisor:
    if _supervisor is None:
        raise RuntimeError("Supervisor not initialised — call init_supervisor() first")
    return _supervisor


def init_supervisor(repo_root: Path, dashboard_dir: Optional[Path] = None) -> Supervisor:
    global _supervisor
    _supervisor = Supervisor(repo_root, dashboard_dir)
    return _supervisor
