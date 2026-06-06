#!/usr/bin/env bash
# Daily factor refresh — regenerate factor_values_*.parquet + regime_*.json
# from fresh data.
#
# Re-runs the research pipeline (stage0a features + stage1 factors + stage2.5
# regime) for ALL configured symbols (currently btc + eth). The trader reads
# research/manifests/factor_values_eth.parquet AND regime_eth.json via its
# /repo:ro mount, so no container restart is needed.
#
# stage2.5 regime matters for regime-overlay strategies (e.g. eth_s5_half_size,
# whose bull/bear masking IS its alpha): without a daily regime refresh the live
# signal ffills the last stored label forever and the overlay silently decays.
#
# Exit 0 on success; non-zero if any stage fails (old outputs are left intact).
#
# Optional env:
#   RESEARCH_VENV  path to a venv to activate (…/bin/activate). If unset, the
#                  `python` already on PATH is used.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESEARCH_DIR="$REPO_ROOT/research"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "[$(ts)] refresh_factors: start"

if [[ -n "${RESEARCH_VENV:-}" ]]; then
  # shellcheck disable=SC1090
  source "$RESEARCH_VENV/bin/activate"
fi

cd "$RESEARCH_DIR"

echo "[$(ts)] stage0a_features…"
python -m pipeline.stage0a_features

echo "[$(ts)] stage1_factors…"
python -m pipeline.stage1_factors

echo "[$(ts)] stage2_5_regime…"
python -m pipeline.stage2_5_regime

echo "[$(ts)] refresh_factors: OK"
