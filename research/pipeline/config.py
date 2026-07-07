"""
research/pipeline/config.py
────────────────────────────
Loads and validates research/research_config.yaml.

Every stage runner should import this module and call load_config() to obtain
a ResearchConfig object instead of hardcoding parameters.

Usage
-----
    from pipeline.config import load_config

    cfg = load_config()
    for sym in cfg.symbols:
        prefix = sym.prefix          # e.g. "btc_"
        okx    = sym.okx_swap        # e.g. "BTC-USDT-SWAP"
        bybit  = sym.ccxt_bybit      # e.g. "BTC/USDT:USDT"
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import yaml

# The YAML file lives at  <repo-root>/research/research_config.yaml.
# This module is at       <repo-root>/research/pipeline/config.py.
# So repo root = two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "research" / "research_config.yaml"

# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class SymbolConfig:
    """Per-symbol seed parameters."""

    name: str          # short lowercase name used as filename prefix (e.g. "btc")
    okx_swap: str      # OKX perpetual swap ticker (e.g. "BTC-USDT-SWAP")
    ccxt_bybit: str    # ccxt-unified Bybit ticker  (e.g. "BTC/USDT:USDT")

    @property
    def prefix(self) -> str:
        """Filename/directory prefix, guaranteed lowercase with trailing underscore (e.g. 'btc_')."""
        return self.name + "_"

    @property
    def binance_usdt(self) -> str:
        """Binance USDT-perp ticker (e.g. 'BTCUSDT') for the daily-metrics archive."""
        return f"{self.name.upper()}USDT"

    @property
    def massive_usd(self) -> str:
        """Massive (Polygon) USD spot ticker (e.g. 'X:BTCUSD') for cross-venue premium.

        Note: no-coverage coins (e.g. BNB on US venues) still return a ticker;
        the fetch then returns None and the factor is simply skipped.
        """
        return f"X:{self.name.upper()}USD"


@dataclasses.dataclass(frozen=True)
class FeesConfig:
    """Transaction-cost seed values written into run config.json by setup_*.py."""

    maker_rate: float
    taker_rate: float
    slippage: float


@dataclasses.dataclass(frozen=True)
class ResearchConfig:
    """Top-level config object returned by load_config()."""

    symbols: tuple[SymbolConfig, ...]
    period: int              # days
    interval: str            # candle bar string, e.g. "1H"
    data_source: str         # e.g. "okx"
    engine: str              # backtest engine mode, e.g. "daily"
    fees: FeesConfig
    horizons_h: tuple[int, ...]    # forward-return horizons in hours
    discovery_cache_days: int = 7  # stage-0 cache TTL in days (0 = disabled)
    # ── Walk-forward train/OOS split (optional) ──────────────────────────────
    # ISO date (YYYY-MM-DD). When set, the backtest period is split into an
    # in-sample TRAIN window [period_start, oos_start) used for parameter tuning,
    # and a held-out OOS window [oos_start, today] used only for final validation.
    # None = no split (legacy: stages tune/backtest on the full period).
    oos_start: str | None = None
    # ── Window freeze + final-holdout cap (optional) ─────────────────────────
    # ISO date (YYYY-MM-DD). When set, this is the frozen "today" anchor used
    # for ALL window math (see resolve_anchor_date() in stage3_backtest.py),
    # so a research iteration cycle can share one fixed reference date across
    # multiple runs instead of drifting with date.today(). None = legacy
    # behavior (today = date.today()).
    window_end: str | None = None
    # ISO date (YYYY-MM-DD). When set, caps the resolved anchor date at
    # holdout_start - 1 day, UNCONDITIONALLY, so no pipeline window can ever
    # reach into the reserved final-holdout period. That data is spent
    # exactly once, later, by a separate manual CLI (final_holdout.py), not
    # part of the iterative pipeline. None = no cap (legacy behavior).
    final_holdout_start: str | None = None
    # When True, the pipeline suppresses realistic-cost keys so the engine uses
    # its hardcoded legacy defaults (byte-for-byte reproduction of old runs).
    # Default False = realistic costs (agy Option B). Debug backdoor only.
    legacy_costs: bool = False
    # ── Evidence-driven indicator pool (Task 1.2) ────────────────────────────
    indicator_pool: tuple[str, ...] = (
        # momentum
        "rsi_14", "macd_diff", "roc_10", "stoch_k",
        # trend
        "ema_cross_9_21", "sma_cross_10_30", "adx_14",
        # volatility
        "atr_14", "bb_width_20", "rolling_std_20",
        # volume
        "obv", "mfi_14", "volume_zscore_20",
    )
    feature_store_path: str = "research/manifests"   # relative to repo root
    append_coverage_threshold: float = 0.5           # min non-NaN fraction to accept a feature column
    # ── Strategy-level lag-stress (stage3 --lag-stress) ───────────────────────
    lag_stress_bars: tuple[int, ...] = (1, 2)  # entry-delay stress bars for stage3 --lag-stress

    # ── Convenience helpers ──────────────────────────────────────────────────

    def symbol_names(self) -> list[str]:
        """Return the short lowercase name for each symbol (e.g. ['btc', 'eth'])."""
        return [s.name for s in self.symbols]

    def symbol_prefix_list(self) -> list[str]:
        """Return the underscore-suffixed prefix for each symbol (e.g. ['btc_', 'eth_'])."""
        return [s.prefix for s in self.symbols]


# ─── Required keys (top-level) ────────────────────────────────────────────────

_REQUIRED_TOP_LEVEL = {"symbols", "period", "interval", "data_source", "engine", "fees", "horizons_h"}
_REQUIRED_SYMBOL_KEYS = {"name", "okx_swap", "ccxt_bybit"}
_REQUIRED_FEE_KEYS = {"maker_rate", "taker_rate", "slippage"}


# ─── Symbol filter ───────────────────────────────────────────────────────────

def _apply_symbol_filter(cfg: ResearchConfig) -> ResearchConfig:
    """If RESEARCH_ONLY_SYMBOL is set, return a copy with symbols filtered to it.

    Lets the dashboard run the pipeline for a single symbol without per-CLI flags.
    Unset -> unchanged (all symbols). Unknown -> ValueError listing valid names.
    """
    only = os.environ.get("RESEARCH_ONLY_SYMBOL", "").strip()
    if not only:
        return cfg
    matches = [s for s in cfg.symbols if s.name == only]
    if not matches:
        valid = [s.name for s in cfg.symbols]
        raise ValueError(
            f"RESEARCH_ONLY_SYMBOL={only!r} matches no config symbol; valid: {valid}"
        )
    return dataclasses.replace(cfg, symbols=tuple(matches))


# ─── Interval override ────────────────────────────────────────────────────────

# Sub-hour profile constants — factors and forward-return horizons applied when
# RESEARCH_INTERVAL is a sub-hour value. Names must exist in _INDICATOR_DISPATCH.
_INTRADAY_FACTORS: tuple[str, ...] = (
    "mom_4", "mom_8", "mom_16",
    "rvol_ratio_8_32", "range_expansion_16", "volume_zscore_8",
)
_INTRADAY_HORIZONS_H: tuple[int, ...] = (1, 2, 4, 8, 24)


def _apply_interval_override(cfg: ResearchConfig) -> ResearchConfig:
    """If RESEARCH_INTERVAL is set, return a copy reflecting that candle interval.

    For "1H" only the interval label changes (zero regression). For a sub-hour
    interval the full intraday profile applies: feature store namespaced by
    interval, short intraday forward-return horizons, and the intraday OHLCV
    factors appended to the pool. Unset -> unchanged. Unknown -> ValueError.
    """
    iv = os.environ.get("RESEARCH_INTERVAL", "").strip()
    if not iv:
        return cfg
    from lib.timeframe import SUPPORTED_INTERVALS

    if iv not in SUPPORTED_INTERVALS:
        raise ValueError(
            f"RESEARCH_INTERVAL={iv!r} is not supported; "
            f"valid: {sorted(SUPPORTED_INTERVALS)}"
        )
    changes: dict = {"interval": iv}
    if iv != "1H":
        changes["feature_store_path"] = f"{cfg.feature_store_path.rstrip('/')}/{iv}"
        changes["horizons_h"] = _INTRADAY_HORIZONS_H
        changes["indicator_pool"] = tuple(cfg.indicator_pool) + tuple(
            f for f in _INTRADAY_FACTORS if f not in cfg.indicator_pool
        )
    return dataclasses.replace(cfg, **changes)


# ─── Loader ──────────────────────────────────────────────────────────────────

def load_config(path: Path | str | None = None) -> ResearchConfig:
    """Load and validate research_config.yaml, returning a ResearchConfig.

    Parameters
    ----------
    path:
        Path to the YAML file.  Defaults to
        ``<repo-root>/research/research_config.yaml``.
        Pass an explicit path in tests or CI to load fixture configs.

    Raises
    ------
    FileNotFoundError
        If the YAML file does not exist at the resolved path.
    TypeError
        If the YAML root is not a mapping, or a required block has the wrong type.
    KeyError
        If a required top-level key, symbol key, or fee key is absent.
    ValueError
        If a numeric field is out of range, or duplicate symbol names are found.
    """
    resolved = Path(path) if path is not None else _DEFAULT_CONFIG_PATH

    if not resolved.exists():
        raise FileNotFoundError(
            f"research_config.yaml not found at: {resolved}\n"
            "Create the file or pass an explicit path to load_config()."
        )

    with resolved.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    # C1: guard against empty or non-mapping YAML
    if not isinstance(raw, dict):
        raise TypeError(
            f"research_config.yaml must be a YAML mapping at the top level, got {type(raw).__name__}."
        )

    # ── Top-level key validation ─────────────────────────────────────────────
    missing_top = _REQUIRED_TOP_LEVEL - raw.keys()
    if missing_top:
        raise KeyError(
            f"research_config.yaml is missing required key(s): {sorted(missing_top)}"
        )

    # ── symbols ──────────────────────────────────────────────────────────────
    raw_symbols = raw["symbols"]
    if not isinstance(raw_symbols, list):
        raise TypeError("'symbols' in research_config.yaml must be a list.")
    if len(raw_symbols) == 0:
        raise ValueError("'symbols' list must not be empty.")

    symbol_configs: list[SymbolConfig] = []
    for i, entry in enumerate(raw_symbols):
        # I1: guard against non-mapping symbol entries
        if not isinstance(entry, dict):
            raise TypeError(
                f"symbols[{i}] must be a YAML mapping, got {type(entry).__name__}."
            )
        missing_sym = _REQUIRED_SYMBOL_KEYS - entry.keys()
        if missing_sym:
            raise KeyError(
                f"symbols[{i}] is missing required key(s): {sorted(missing_sym)}"
            )
        symbol_configs.append(
            SymbolConfig(
                name=str(entry["name"]).lower(),
                okx_swap=str(entry["okx_swap"]),
                ccxt_bybit=str(entry["ccxt_bybit"]),
            )
        )

    # I3: duplicate symbol names
    names = [s.name for s in symbol_configs]
    seen: set[str] = set()
    duplicates = [n for n in names if n in seen or seen.add(n)]  # type: ignore[func-returns-value]
    if duplicates:
        raise ValueError(
            f"Duplicate symbol name(s) found in research_config.yaml: {sorted(set(duplicates))}"
        )

    # ── fees ─────────────────────────────────────────────────────────────────
    raw_fees = raw["fees"]
    # I1: guard against non-mapping fees block
    if not isinstance(raw_fees, dict):
        raise TypeError(
            f"'fees' in research_config.yaml must be a mapping, got {type(raw_fees).__name__}."
        )
    missing_fees = _REQUIRED_FEE_KEYS - raw_fees.keys()
    if missing_fees:
        raise KeyError(
            f"fees block is missing required key(s): {sorted(missing_fees)}"
        )

    maker_rate = float(raw_fees["maker_rate"])
    taker_rate = float(raw_fees["taker_rate"])
    slippage = float(raw_fees["slippage"])

    # I2: range validation for fee rates
    if maker_rate < 0:
        raise ValueError(f"fees.maker_rate must be >= 0, got {maker_rate}.")
    if taker_rate < 0:
        raise ValueError(f"fees.taker_rate must be >= 0, got {taker_rate}.")
    if slippage < 0:
        raise ValueError(f"fees.slippage must be >= 0, got {slippage}.")

    fees = FeesConfig(
        maker_rate=maker_rate,
        taker_rate=taker_rate,
        slippage=slippage,
    )

    # ── horizons_h ───────────────────────────────────────────────────────────
    raw_horizons = raw["horizons_h"]
    if not isinstance(raw_horizons, list):
        raise TypeError("'horizons_h' in research_config.yaml must be a list.")
    horizons = [int(h) for h in raw_horizons]

    # I2: range validation for horizons
    for h in horizons:
        if h <= 0:
            raise ValueError(
                f"Every value in horizons_h must be > 0, got {h}."
            )

    # ── period ───────────────────────────────────────────────────────────────
    period = int(raw["period"])
    # I2: range validation for period
    if period <= 0:
        raise ValueError(f"'period' must be > 0, got {period}.")

    # ── discovery_cache_days ─────────────────────────────────────────────────
    discovery_cache_days = int(raw.get("discovery_cache_days", 7))
    if discovery_cache_days < 0:
        raise ValueError(
            f"'discovery_cache_days' must be >= 0, got {discovery_cache_days}."
        )

    # ── indicator_pool ───────────────────────────────────────────────────────
    _default_indicator_pool = (
        "rsi_14", "macd_diff", "roc_10", "stoch_k",
        "ema_cross_9_21", "sma_cross_10_30", "adx_14",
        "atr_14", "bb_width_20", "rolling_std_20",
        "obv", "mfi_14", "volume_zscore_20",
    )
    raw_pool = raw.get("indicator_pool", None)
    if raw_pool is None:
        indicator_pool = _default_indicator_pool
    else:
        if not isinstance(raw_pool, list):
            raise TypeError("'indicator_pool' in research_config.yaml must be a list of strings.")
        indicator_pool = tuple(str(s) for s in raw_pool)

    # ── lag_stress_bars ──────────────────────────────────────────────────────
    raw_lag_stress_bars = raw.get("lag_stress_bars", None)
    if raw_lag_stress_bars is None:
        lag_stress_bars = (1, 2)
    else:
        if not isinstance(raw_lag_stress_bars, list):
            raise TypeError("'lag_stress_bars' in research_config.yaml must be a list of ints.")
        lag_stress_bars = tuple(int(x) for x in raw_lag_stress_bars)

    # ── feature_store_path ───────────────────────────────────────────────────
    feature_store_path = str(raw.get("feature_store_path", "research/manifests"))

    # ── append_coverage_threshold ────────────────────────────────────────────
    append_coverage_threshold = float(raw.get("append_coverage_threshold", 0.5))
    if not 0.0 <= append_coverage_threshold <= 1.0:
        raise ValueError(
            f"'append_coverage_threshold' must be between 0.0 and 1.0, got {append_coverage_threshold}."
        )

    # ── oos_start (optional walk-forward split) ──────────────────────────────
    oos_start_raw = raw.get("oos_start")
    oos_start = None
    if oos_start_raw is not None:
        oos_start = str(oos_start_raw)
        # Validate ISO date format (YYYY-MM-DD).
        from datetime import date as _date

        try:
            _date.fromisoformat(oos_start)
        except ValueError as exc:
            raise ValueError(
                f"'oos_start' must be an ISO date (YYYY-MM-DD), got {oos_start!r}."
            ) from exc

    # ── window_end (optional frozen window anchor) ───────────────────────────
    window_end_raw = raw.get("window_end")
    window_end = None
    if window_end_raw is not None:
        window_end = str(window_end_raw)
        # Validate ISO date format (YYYY-MM-DD).
        from datetime import date as _date

        try:
            _date.fromisoformat(window_end)
        except ValueError as exc:
            raise ValueError(
                f"'window_end' must be an ISO date (YYYY-MM-DD), got {window_end!r}."
            ) from exc

    # ── final_holdout_start (optional final-holdout cap) ─────────────────────
    final_holdout_start_raw = raw.get("final_holdout_start")
    final_holdout_start = None
    if final_holdout_start_raw is not None:
        final_holdout_start = str(final_holdout_start_raw)
        # Validate ISO date format (YYYY-MM-DD).
        from datetime import date as _date

        try:
            _date.fromisoformat(final_holdout_start)
        except ValueError as exc:
            raise ValueError(
                f"'final_holdout_start' must be an ISO date (YYYY-MM-DD), got {final_holdout_start!r}."
            ) from exc

    # ── legacy_costs (optional debug backdoor) ───────────────────────────────
    legacy_costs = bool(raw.get("legacy_costs", False))

    cfg = ResearchConfig(
        symbols=tuple(symbol_configs),
        period=period,
        interval=str(raw["interval"]),
        data_source=str(raw["data_source"]),
        engine=str(raw["engine"]),
        fees=fees,
        horizons_h=tuple(horizons),
        discovery_cache_days=discovery_cache_days,
        indicator_pool=indicator_pool,
        feature_store_path=feature_store_path,
        append_coverage_threshold=append_coverage_threshold,
        oos_start=oos_start,
        window_end=window_end,
        final_holdout_start=final_holdout_start,
        legacy_costs=legacy_costs,
        lag_stress_bars=lag_stress_bars,
    )
    return _apply_interval_override(_apply_symbol_filter(cfg))
