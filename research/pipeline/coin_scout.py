"""CLI: probe data-source coverage for candidate coins.

    python -m research.pipeline.coin_scout                 # default candidate set
    python -m research.pipeline.coin_scout --coins bnb,xrp

Writes research/manifests/coin_scout.json and prints a verdict table plus
paste-ready research_config.yaml blocks for GO coins.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Bootstrap research/ onto sys.path so ``lib.*`` imports resolve from any CWD.
_PIPELINE_DIR = Path(__file__).resolve().parent
_RESEARCH_DIR = _PIPELINE_DIR.parent
if str(_RESEARCH_DIR) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_DIR))

from lib.coin_scout import (  # noqa: E402
    CoinVerdict,
    ScoutReport,
    format_table,
    go_coins_yaml,
    report_to_dict,
    scout_coins,
)

_REPO_ROOT = _RESEARCH_DIR.parent
DEFAULT_CANDIDATES = ["bnb", "xrp", "doge", "ada", "ltc", "bch"]
_DEFAULT_OUT = _REPO_ROOT / "research" / "manifests" / "coin_scout.json"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Coin feasibility scout")
    p.add_argument("--coins", default="", help="comma-separated coin names; empty = default set")
    p.add_argument("--out", default=str(_DEFAULT_OUT), help="output JSON path")
    args = p.parse_args(argv)

    names = [c for c in args.coins.split(",") if c.strip()] or DEFAULT_CANDIDATES
    report = scout_coins(names)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report_to_dict(report), indent=2, ensure_ascii=False), encoding="utf-8")

    print(format_table(report))
    go_yaml = go_coins_yaml(report)
    if go_yaml:
        print("\n# GO coins — paste into research_config.yaml under symbols:\n")
        print(go_yaml)
    print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
