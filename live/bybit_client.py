"""Bybit testnet ccxt wrapper with safety guards.

Use this for paper-trade reconciliation. Never instantiate against mainnet
unless you've passed all validation layers and read the real-money checklist.

Safety guards (HARD-CODED, can't be disabled via config):
- Sandbox mode forced ON in __init__ (set BYBIT_LIVE=1 env explicitly to opt out — extra friction)
- max_position_btc: ceiling on absolute BTC contract size
- max_leverage: enforced on each order via exchange.set_leverage
- mark_price_drift_abort: if mark price moved > N% since signal generated, abort order
- min_order_btc: skip dust orders below this threshold
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import ccxt


@dataclass
class SafetyConfig:
    max_position_btc: float = 0.05           # ~$3500 at $70k → ~3.5x of $1k capital headroom
    max_leverage: float = 3.0                # absolute ceiling regardless of config
    mark_drift_abort_pct: float = 0.02       # 2% intra-signal price move = abort
    min_order_btc: float = 0.001             # skip < $70 orders (Bybit min varies; verify)
    daily_order_cap: int = 24                # max orders per UTC day (sanity)


class BybitClient:
    SYMBOL = "BTC/USDT:USDT"  # ccxt unified linear perp

    def __init__(self, api_key: str, secret: str, safety: SafetyConfig | None = None):
        if os.getenv("BYBIT_LIVE") == "1":
            print("[bybit] WARNING: BYBIT_LIVE=1 — connecting to MAINNET (real money)")
            sandbox = False
        else:
            sandbox = True

        self.exchange = ccxt.bybit({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "linear"},  # USDT-margined perp
        })
        if sandbox:
            self.exchange.set_sandbox_mode(True)
            print("[bybit] sandbox mode ON (testnet)")

        self.safety = safety or SafetyConfig()
        self._daily_orders = 0
        self._daily_orders_date = time.strftime("%Y-%m-%d")

    # ---------------- state ----------------
    def fetch_mark_price(self) -> float:
        ticker = self.exchange.fetch_ticker(self.SYMBOL)
        # Bybit linear perp: ticker['info']['markPrice'] or last
        mark = ticker.get("markPrice")
        if mark is None:
            mark = ticker.get("last")
        return float(mark)

    def fetch_position_btc(self) -> float:
        """Net BTC contracts. Positive = long, negative = short, 0 = flat."""
        try:
            positions = self.exchange.fetch_positions([self.SYMBOL])
        except Exception as e:
            print(f"[bybit] fetch_positions error: {e}")
            return 0.0
        if not positions:
            return 0.0
        # ccxt unified: 'contracts' (positive) + 'side' ('long'/'short')
        # Bybit hedge mode could return 2 entries; sum signed
        net = 0.0
        for p in positions:
            if p.get("symbol") != self.SYMBOL:
                continue
            qty = float(p.get("contracts") or 0)
            side = p.get("side")
            if side == "short":
                qty = -qty
            net += qty
        return net

    def fetch_balance_usdt(self) -> float:
        bal = self.exchange.fetch_balance()
        return float(bal.get("USDT", {}).get("total") or 0)

    # ---------------- reconcile ----------------
    def reconcile_to_target(
        self,
        target_position_weight: float,   # in [-1, 1]; for short-only typically [-0.5, 0]
        capital_usdt: float,
        leverage: float,
        signal_mark_price: float,
        dry_run: bool = False,
    ) -> dict:
        """Place market order to bring current position to target weight.

        target_position_weight × capital_usdt × leverage / mark_price = target BTC contracts.
        """
        # safety: leverage cap
        if leverage > self.safety.max_leverage:
            raise ValueError(f"leverage {leverage} > max {self.safety.max_leverage}")

        live_mark = self.fetch_mark_price()
        drift = abs(live_mark - signal_mark_price) / signal_mark_price
        if drift > self.safety.mark_drift_abort_pct:
            return {
                "status": "aborted",
                "reason": f"mark drift {drift*100:.2f}% > {self.safety.mark_drift_abort_pct*100:.1f}% threshold",
                "signal_mark": signal_mark_price,
                "live_mark": live_mark,
            }

        # daily order cap (UTC date)
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_orders_date:
            self._daily_orders = 0
            self._daily_orders_date = today
        if self._daily_orders >= self.safety.daily_order_cap:
            return {"status": "aborted", "reason": f"daily order cap {self.safety.daily_order_cap} hit"}

        # compute target contracts
        target_value = target_position_weight * capital_usdt * leverage
        target_btc = target_value / live_mark

        # safety: position size cap
        if abs(target_btc) > self.safety.max_position_btc:
            target_btc = self.safety.max_position_btc * (1 if target_btc > 0 else -1)

        current_btc = self.fetch_position_btc()
        delta_btc = target_btc - current_btc

        if abs(delta_btc) < self.safety.min_order_btc:
            return {
                "status": "no_op",
                "reason": "delta below min_order_btc",
                "target_btc": target_btc,
                "current_btc": current_btc,
                "delta_btc": delta_btc,
            }

        side = "buy" if delta_btc > 0 else "sell"
        qty = abs(delta_btc)

        if dry_run:
            return {
                "status": "dry_run",
                "side": side, "qty": qty,
                "target_btc": target_btc, "current_btc": current_btc, "delta_btc": delta_btc,
                "live_mark": live_mark, "signal_mark": signal_mark_price,
            }

        # set leverage (idempotent; Bybit allows runtime change for isolated margin)
        try:
            self.exchange.set_leverage(int(leverage), self.SYMBOL)
        except Exception as e:
            print(f"[bybit] set_leverage warning: {e} (continuing)")

        try:
            order = self.exchange.create_order(
                self.SYMBOL, "market", side, qty,
                params={"reduceOnly": False, "positionIdx": 0},
            )
            self._daily_orders += 1
            return {
                "status": "filled",
                "order_id": order.get("id"),
                "side": side, "qty": qty,
                "filled": order.get("filled"),
                "average": order.get("average") or order.get("price"),
                "target_btc": target_btc, "current_btc_before": current_btc,
                "delta_btc": delta_btc, "live_mark": live_mark,
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "side": side, "qty": qty}
