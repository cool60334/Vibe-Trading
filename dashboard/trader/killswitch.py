"""Kill switch — drawdown-based auto-pause and auto-terminate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional, Tuple


KillDecision = Literal["ok", "pause", "terminate"]


class KillSwitch:
    """Track peak equity and trigger pause/terminate on drawdown breach.

    Args:
        initial_equity: Starting equity (sets the first peak).
        pause_dd: Drawdown fraction that pauses trading (default 5%).
        terminate_dd: Drawdown fraction that terminates trading (default 7%).
        state_path: When set, the peak is persisted here and reloaded on
            construction so drawdown is measured from the true peak across
            restarts instead of re-baselining to current equity.
    """

    def __init__(
        self,
        initial_equity: float,
        pause_dd: float = 0.05,
        terminate_dd: float = 0.07,
        state_path: Optional["str | Path"] = None,
    ) -> None:
        self.initial_equity = initial_equity
        self.peak_equity = initial_equity
        self.pause_dd = pause_dd
        self.terminate_dd = terminate_dd
        self.state_path = Path(state_path) if state_path else None
        if self.state_path is not None and self.state_path.exists():
            self._load_state()

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"peak_equity": self.peak_equity}), encoding="utf-8"
        )

    def _load_state(self) -> None:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.peak_equity = float(data["peak_equity"])

    def check(self, current_equity: float) -> Tuple[KillDecision, str | None]:
        """Evaluate current equity against thresholds.

        Updates internal peak. Returns (decision, reason) where decision is:
          "ok"        — continue trading
          "pause"     — drawdown >= pause_dd, stop new orders
          "terminate" — drawdown >= terminate_dd, shut down process

        Args:
            current_equity: Current total equity.

        Returns:
            (decision, reason_string or None)
        """
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
            self._save_state()

        if self.peak_equity <= 0:
            return "ok", None

        dd = (self.peak_equity - current_equity) / self.peak_equity

        if dd >= self.terminate_dd:
            return (
                "terminate",
                f"Drawdown {dd:.2%} reached terminate threshold {self.terminate_dd:.2%}",
            )
        if dd >= self.pause_dd:
            return (
                "pause",
                f"Drawdown {dd:.2%} reached pause threshold {self.pause_dd:.2%}",
            )
        return "ok", None

    def current_drawdown(self, current_equity: float) -> float:
        """Return current drawdown as a positive fraction (0.0 = no DD)."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - current_equity) / self.peak_equity)
