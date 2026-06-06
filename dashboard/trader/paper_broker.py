"""Paper broker — mainnet market data, local virtual account (dry-run).

Reads REAL mainnet prices via ccxt public endpoints (no API key needed) and
simulates fills locally instead of sending live orders. PnL is marked to the
real market, with taker fee, slippage, and 8h funding all charged so the dry
run tracks a real perpetual account as closely as possible.

Same public interface as ``Broker`` (the live/testnet broker) so the trading
loop, supervisor, and dashboard are agnostic to which one is in use.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

# Bybit USDT-perp funding settles every 8h at 00:00 / 08:00 / 16:00 UTC.
_FUNDING_HOURS = (0, 8, 16)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _funding_boundaries_between(start: datetime, end: datetime) -> list[datetime]:
    """Funding settlement timestamps in the half-open interval (start, end]."""
    start = _to_utc(start)
    end = _to_utc(end)
    if end <= start:
        return []
    out: list[datetime] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        for h in _FUNDING_HOURS:
            b = day.replace(hour=h)
            if start < b <= end:
                out.append(b)
        day += timedelta(days=1)
    return out


class PaperBroker:
    """Virtual perpetual account fed by real mainnet market data.

    Args:
        exchange: ccxt-like object providing public ``fetch_ticker``,
            ``fetch_order_book``, ``fetch_funding_rate`` (and ``fetch_ohlcv``,
            used by the signal layer via the ``.exchange`` attribute).
        equity: Starting virtual USDT balance.
        taker_fee: Taker fee fraction per fill (Bybit USDT-perp ≈ 0.00055).
        slippage_bps: Adverse slippage in basis points applied to each fill.
        clock: Returns the current UTC time; injectable for tests.
        state_path: When set, cash / position / funding cursor are persisted
            here and reloaded on construction so the virtual account survives
            process restarts instead of resetting to *equity*.
    """

    def __init__(
        self,
        exchange,
        *,
        equity: float = 10000.0,
        taker_fee: float = 0.00055,
        slippage_bps: float = 5.0,
        clock: Optional[Callable[[], datetime]] = None,
        state_path: Optional["str | Path"] = None,
    ) -> None:
        self.exchange = exchange
        self.cash = float(equity)
        self.taker_fee = float(taker_fee)
        self.slippage = float(slippage_bps) / 10000.0
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self.position: Optional[dict] = None  # {side, size, entry_price, symbol}
        self._last_funding_check = _to_utc(self._clock())
        self.state_path = Path(state_path) if state_path else None
        if self.state_path is not None and self.state_path.exists():
            self._load_state()

    # ── State persistence ─────────────────────────────────────────────────────

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        data = {
            "cash": self.cash,
            "position": self.position,
            "last_funding_check": self._last_funding_check.isoformat(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_state(self) -> None:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.cash = float(data["cash"])
        self.position = data.get("position")
        lf = data.get("last_funding_check")
        if lf:
            self._last_funding_check = _to_utc(datetime.fromisoformat(lf))

    # ── Pricing helpers ───────────────────────────────────────────────────────

    def _mark_price(self, symbol: str) -> float:
        return float(self.exchange.fetch_ticker(symbol)["last"])

    def _fill_price(self, symbol: str, side: str) -> float:
        ob = self.exchange.fetch_order_book(symbol)
        if side == "buy":
            ask = float(ob["asks"][0][0])
            return ask * (1 + self.slippage)
        bid = float(ob["bids"][0][0])
        return bid * (1 - self.slippage)

    # ── Account ──────────────────────────────────────────────────────────────

    def get_equity(self) -> float:
        """Virtual USDT equity = cash + unrealized PnL, funding accrued first."""
        self.accrue_funding(self._clock())
        if self.position is None:
            return self.cash
        mark = self._mark_price(self.position["symbol"])
        sign = 1 if self.position["side"] == "long" else -1
        unrealized = (mark - self.position["entry_price"]) * self.position["size"] * sign
        return self.cash + unrealized

    def get_position(self, symbol: str) -> Optional[dict]:
        """Return open position for *symbol*, or None if flat."""
        if self.position is None:
            return None
        return {
            "side": self.position["side"],
            "size": self.position["size"],
            "entry_price": self.position["entry_price"],
        }

    # ── Funding ──────────────────────────────────────────────────────────────

    def accrue_funding(self, now: datetime) -> float:
        """Charge/credit funding for each 8h boundary since the last check.

        Longs pay when the funding rate is positive; shorts receive. Returns
        the net cash delta (negative = paid out).
        """
        now = _to_utc(now)
        if self.position is None:
            self._last_funding_check = now
            self._save_state()
            return 0.0

        boundaries = _funding_boundaries_between(self._last_funding_check, now)
        self._last_funding_check = now
        if not boundaries:
            self._save_state()
            return 0.0

        rate = float(self.exchange.fetch_funding_rate(self.position["symbol"])["fundingRate"])
        notional = self._mark_price(self.position["symbol"]) * self.position["size"]
        sign = 1 if self.position["side"] == "long" else -1
        total = 0.0
        for _ in boundaries:
            payment = rate * notional * sign  # long pays when rate>0
            self.cash -= payment
            total += payment
        self._save_state()
        return total

    # ── Orders ───────────────────────────────────────────────────────────────

    def place_market_order(self, symbol: str, side: str, qty: float) -> dict:
        """Simulate a market fill at the order-book touch + slippage.

        Charges a taker fee on every fill. Opening from flat records the
        position; an order opposing an open position closes it and realizes PnL.
        """
        self.accrue_funding(self._clock())
        fill = self._fill_price(symbol, side)
        fee = fill * qty * self.taker_fee
        self.cash -= fee

        if self.position is None:
            self.position = {
                "side": "long" if side == "buy" else "short",
                "size": qty,
                "entry_price": fill,
                "symbol": symbol,
            }
        else:
            pos = self.position
            closing = (
                (pos["side"] == "long" and side == "sell")
                or (pos["side"] == "short" and side == "buy")
            )
            if closing:
                sign = 1 if pos["side"] == "long" else -1
                realized = (fill - pos["entry_price"]) * pos["size"] * sign
                self.cash += realized
                self.position = None
            else:
                # Same-direction order: replace (loop never pyramids).
                self.position = {
                    "side": "long" if side == "buy" else "short",
                    "size": qty,
                    "entry_price": fill,
                    "symbol": symbol,
                }

        self._save_state()
        return {"average": fill, "side": side, "amount": qty, "fee": fee}

    def close_position(self, symbol: str) -> Optional[dict]:
        """Close the open position for *symbol*, if any."""
        pos = self.get_position(symbol)
        if pos is None:
            return None
        close_side = "sell" if pos["side"] == "long" else "buy"
        return self.place_market_order(symbol, close_side, pos["size"])

    def get_ticker(self, symbol: str) -> dict:
        """Return current ticker (bid/ask/last) for *symbol*."""
        return self.exchange.fetch_ticker(symbol)
