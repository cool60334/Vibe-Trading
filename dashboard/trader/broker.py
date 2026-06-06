"""Bybit broker — ccxt wrapper for order execution.

Three trading modes, selected by ``make_broker`` / ``TRADING_MODE``:
  paper   — mainnet market data, local virtual fills (no keys, no real orders)
  testnet — Bybit sandbox, real orders on fake books (engineering/debug only)
  live    — mainnet, REAL orders (needs keys; use with care)

``testnet``/``live`` read credentials from the environment:
  BYBIT_API_KEY
  BYBIT_API_SECRET
"""

from __future__ import annotations

import os
from typing import Optional

import ccxt

from trader.paper_broker import PaperBroker


def _make_exchange(sandbox: bool, *, require_keys: bool = True) -> ccxt.bybit:
    """Build a ccxt Bybit client.

    Args:
        sandbox: True → testnet endpoints; False → mainnet.
        require_keys: Raise if API keys are absent. Public-data-only callers
            (paper mode) pass False — mainnet OHLCV/ticker/book/funding are
            public endpoints.
    """
    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if require_keys and (not api_key or not api_secret):
        raise EnvironmentError("BYBIT_API_KEY and BYBIT_API_SECRET must be set")

    exchange = ccxt.bybit(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
        }
    )
    exchange.set_sandbox_mode(sandbox)
    return exchange


def make_broker(mode: Optional[str] = None):
    """Construct the broker for *mode* (defaults to ``$TRADING_MODE`` or paper).

    paper → PaperBroker on mainnet public data; testnet/live → Broker with
    real order execution on the sandbox / mainnet respectively.
    """
    mode = (mode or os.environ.get("TRADING_MODE", "paper")).lower()
    if mode == "paper":
        exchange = _make_exchange(sandbox=False, require_keys=False)
        return PaperBroker(
            exchange,
            equity=float(os.environ.get("PAPER_EQUITY", "10000")),
            taker_fee=float(os.environ.get("PAPER_TAKER_FEE", "0.00055")),
            slippage_bps=float(os.environ.get("PAPER_SLIPPAGE_BPS", "5")),
        )
    if mode == "testnet":
        return Broker(sandbox=True)
    if mode == "live":
        return Broker(sandbox=False)
    raise ValueError(f"unknown TRADING_MODE: {mode!r} (expected paper|testnet|live)")


class Broker:
    """Thin ccxt wrapper for Bybit perpetual trading with REAL order execution.

    ``sandbox=True`` routes to testnet; ``sandbox=False`` to mainnet (live).
    """

    def __init__(self, sandbox: bool = True) -> None:
        self.exchange = _make_exchange(sandbox, require_keys=True)

    # ── Account ──────────────────────────────────────────────────────────────

    def get_equity(self) -> float:
        """Return total USDT equity in the unified trading account."""
        balance = self.exchange.fetch_balance({"type": "swap"})
        usdt = balance.get("USDT", {})
        return float(usdt.get("total", 0.0))

    # ── Position ─────────────────────────────────────────────────────────────

    def get_position(self, symbol: str) -> Optional[dict]:
        """Return open position for *symbol*, or None if flat.

        Returns dict with keys: side ("long"/"short"), size (float), entry_price (float).
        """
        positions = self.exchange.fetch_positions([symbol])
        for pos in positions:
            contracts = float(pos.get("contracts") or 0)
            if contracts > 0:
                return {
                    "side": pos["side"],
                    "size": contracts,
                    "entry_price": float(pos.get("entryPrice") or 0),
                }
        return None

    # ── Orders ───────────────────────────────────────────────────────────────

    def place_market_order(self, symbol: str, side: str, qty: float) -> dict:
        """Place a market order.

        Args:
            symbol: e.g. "BTC/USDT:USDT"
            side: "buy" or "sell"
            qty: Quantity in base asset (contracts).

        Returns:
            ccxt order dict.
        """
        order = self.exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=qty,
        )
        return order

    def close_position(self, symbol: str) -> Optional[dict]:
        """Close the open position for *symbol*, if any.

        Returns the closing order dict, or None if already flat.
        """
        pos = self.get_position(symbol)
        if pos is None:
            return None
        # To close a long, sell; to close a short, buy.
        close_side = "sell" if pos["side"] == "long" else "buy"
        return self.place_market_order(symbol, close_side, pos["size"])

    def get_ticker(self, symbol: str) -> dict:
        """Return current ticker (bid/ask/last) for *symbol*."""
        return self.exchange.fetch_ticker(symbol)
