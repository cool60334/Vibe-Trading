#!/usr/bin/env bash
# Refresh full-span OHLCV parquets for the Foundry — ohlcv_<sym>.parquet written
# next to features_<sym>.parquet for every configured symbol. Mirrors
# refresh_factors.sh. Exit 0 on success; non-zero if any symbol failed (old
# parquets left intact).
#
# Optional env:
#   RESEARCH_VENV        path to a venv activate script (…/bin/activate)
#   RESEARCH_ONLY_SYMBOL restrict to one coin (honored by load_config)
#   RESEARCH_INTERVAL    candle interval (default 1H)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "[$(ts)] refresh_foundry_ohlcv: start"

if [[ -n "${RESEARCH_VENV:-}" ]]; then
  # shellcheck disable=SC1090
  source "$RESEARCH_VENV/bin/activate"
fi

cd "$REPO_ROOT"           # repo root on path so research.hermes.orchestrator resolves
rc=0
python -m research.pipeline.refresh_ohlcv "$@" || rc=$?

echo "[$(ts)] refresh_foundry_ohlcv: done (exit $rc)"
exit "$rc"
