"""Factor induction (轉正): move a vetted Foundry factor's compute() code into
the production factor library. Human-gated (--confirm), re-validated hard.

Like promote.py, this is a privileged production write and is NOT exported from
research.hermes.__init__; it runs only when a human invokes it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research.hermes.candidate_store import load_candidate_code, _candidate_path
from research.hermes.errors import HermesGuardError
from research.hermes.evidence_card import VERDICT_CANDIDATE
from research.hermes.evidence_store import load_cards
from research.hermes.sandbox_ast import check_source, UnsafeCodeError
from research.hermes.foundry_bridge import recompute_full_span, reconciles_pre_oos
from research.hermes.forge import pit_check_via_sandbox
from research.hermes.pit import LookaheadError
from research.lib.inducted_factors import inducted_dir


class InductRefused(HermesGuardError, RuntimeError):
    """Induction is not allowed (bad verdict / sha / AST / PIT / determinism / divergence / no confirm)."""


def verify_gates(factor_id: str, symbol: str, manifests_dir) -> str:
    """Load the stored code and run the cheap gates (verdict, sha, AST). Returns
    the code text. Raises InductRefused on any violation."""
    cards = {c.factor_id: c for c in load_cards(symbol, manifests_dir)}
    card = cards.get(factor_id)
    if card is None:
        raise InductRefused(f"no evidence card for {symbol}:{factor_id}")
    if card.verdict != VERDICT_CANDIDATE:
        raise InductRefused(f"{symbol}:{factor_id} verdict={card.verdict!r}, not a candidate")

    code, meta = load_candidate_code(factor_id, symbol, manifests_dir)
    actual = hashlib.sha256(code.encode()).hexdigest()
    if meta.get("code_sha256") and meta["code_sha256"] != actual:
        raise InductRefused(f"{symbol}:{factor_id} code sha mismatch (tamper?)")
    try:
        check_source(code)
    except UnsafeCodeError as exc:
        raise InductRefused(f"{symbol}:{factor_id} failed AST allowlist: {exc}") from exc
    return code


def revalidate(code, factor_id, symbol, panel, run_sandbox, oos_start, manifests_dir) -> None:
    """The heavy re-validation gates. Raises InductRefused on any failure.
      - full-span PIT re-check (no future leak in the non-sandboxed prod path)
      - determinism (compute twice -> identical; catches leaked RNG/global state)
      - path-consistency (recompute's pre-oos == the values Foundry stored, i.e.
        what the strategy was backtested on -> live matches the backtest)."""
    first = recompute_full_span(code, panel, run_sandbox)
    try:
        pit_check_via_sandbox(code, panel, first, run_sandbox)
    except LookaheadError as exc:
        raise InductRefused(f"{symbol}:{factor_id} peeks into the future: {exc}") from exc

    second = recompute_full_span(code, panel, run_sandbox)
    if not first.equals(second):
        raise InductRefused(f"{symbol}:{factor_id} is non-deterministic (compute twice differs)")

    cand_path = _candidate_path(symbol, manifests_dir)
    if not cand_path.exists():
        raise InductRefused(
            f"{symbol}:{factor_id} has no stored candidate values at {cand_path} "
            "(cannot verify path-consistency); refusing to induct")
    stored = pd.read_parquet(cand_path)
    if factor_id not in stored.columns:
        raise InductRefused(
            f"{symbol}:{factor_id} is not a column in the stored candidate values "
            "(cannot verify path-consistency); refusing to induct")
    if not reconciles_pre_oos(first, stored[factor_id], oos_start):
        raise InductRefused(
            f"{symbol}:{factor_id} recompute diverges from the stored Foundry values "
            "(the strategy's backtest would not match live); refusing to induct")


def write_induction(code, factor_id, symbol, fixture, expected, root=None, tests_root=None) -> None:
    d = inducted_dir(symbol, root=root); d.mkdir(parents=True, exist_ok=True)
    (d / f"{factor_id}.py").write_text(code, encoding="utf-8")
    (d / f"{factor_id}.meta.json").write_text(json.dumps({
        "factor_id": factor_id, "symbol": symbol,
        "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "inducted_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    tr = Path(tests_root) if tests_root is not None else \
        (Path(__file__).resolve().parents[2] / "research" / "tests" / "inducted")
    tr.mkdir(parents=True, exist_ok=True)
    fixture.to_parquet(tr / f"{factor_id}_fixture.parquet")
    expected.to_frame("expected").to_parquet(tr / f"{factor_id}_expected.parquet")
    (tr / f"test_{factor_id}.py").write_text(
        "import pandas as pd\n"
        "from pathlib import Path\n"
        "import importlib.util\n\n"
        f"_ID = {factor_id!r}\n_SYM = {symbol!r}\n"
        "_HERE = Path(__file__).resolve().parent\n\n"
        "def test_inducted_logic_unchanged():\n"
        "    fx = pd.read_parquet(_HERE / f'{_ID}_fixture.parquet')\n"
        "    exp = pd.read_parquet(_HERE / f'{_ID}_expected.parquet')['expected']\n"
        "    mod_path = Path(__file__).resolve().parents[2] / 'lib' / 'inducted' / _SYM / f'{_ID}.py'\n"
        "    spec = importlib.util.spec_from_file_location(f'ind_{_SYM}_{_ID}', mod_path)\n"
        "    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "    got = m.compute(fx)\n"
        "    pd.testing.assert_series_equal(got, exp, rtol=1e-5, atol=1e-8, check_names=False)\n",
        encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Induct a vetted Foundry factor into the production library.")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--factor", required=True)
    ap.add_argument("--manifests-dir", default=None)
    ap.add_argument("--image", default="talos-sandbox:test")
    ap.add_argument("--oos-start", default=None)
    ap.add_argument("--confirm", action="store_true", help="required — without it, refuses")
    args = ap.parse_args(argv)

    from research.hermes.foundry_runner import resolve_image_id, load_ohlcv
    from research.hermes.orchestrator import make_run_sandbox
    from research.hermes.sandbox import DockerSandbox
    from research.lib.factor_io import _default_manifests_dir, load_features, _symbol_short
    from pipeline.config import load_config

    mdir = Path(args.manifests_dir) if args.manifests_dir else _default_manifests_dir()
    cfg = load_config()
    oos = args.oos_start or cfg.oos_start
    try:
        code = verify_gates(args.factor, args.symbol, mdir)
        features = load_features(args.symbol, manifests_dir=mdir)
        ohlcv = load_ohlcv(mdir / f"ohlcv_{_symbol_short(args.symbol)}.parquet")
        panel = features.join(ohlcv, how="left")
        sandbox = DockerSandbox(image=resolve_image_id(args.image), timeout_s=120, allow_unpinned=False)
        run_sandbox = make_run_sandbox(sandbox, mdir / "_foundry_scratch")
        revalidate(code, args.factor, args.symbol, panel, run_sandbox, oos, mdir)

        if not args.confirm:
            print(f"[induct] all gates pass for {args.symbol}:{args.factor}. "
                  "Re-run with --confirm to write it into the production library.")
            return 2
        fixture = panel.head(200)
        expected = run_sandbox(code, fixture)
        write_induction(code, args.factor, args.symbol, fixture, expected)
        print(f"[induct] {args.symbol}:{args.factor} inducted. COMMIT the new files; "
              "then stage0a will compute it and the strategy can deploy.")
        return 0
    except InductRefused as exc:
        print(f"[induct] REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
