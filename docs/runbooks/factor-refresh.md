# Runbook — ETH Factor Refresh

Keeps `research/manifests/factor_values_eth.parquet` fresh so the testnet
trader (`eth_s5_half_size`) trades on current factors, not frozen backtest data.

## What runs

`scripts/refresh_factors.sh` re-runs `stage0a_features` + `stage1_factors` for
all configured symbols (btc + eth), rewriting the factor parquets. The trader
container reads them via `/repo:ro` — no restart needed.

## One-time VPS setup

1. Create the research venv and install deps:
   ```bash
   cd /path/to/repo/research
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install pandas pyarrow ccxt requests pyyaml scipy scikit-learn   # core research deps
   ```
2. Seed the parquet once (verifies network + creds):
   ```bash
   RESEARCH_VENV=/path/to/repo/research/.venv \
     bash /path/to/repo/scripts/refresh_factors.sh
   ```
   Confirm the terminal output ends with `refresh_factors: OK`.

## Enable the daily cron

Add to the VPS crontab (`crontab -e`), runs 00:30 UTC daily:

```cron
30 0 * * *  RESEARCH_VENV=/path/to/repo/research/.venv /path/to/repo/scripts/refresh_factors.sh >> /path/to/repo/research/refresh_factors.log 2>&1
```

## Verify freshness

- Newest factor timestamp:
  ```bash
  python -c "import json; print(json.load(open('research/manifests/factor_values_eth.meta.json'))['index_end'])"
  ```
  Should be within ~1 day of now (UTC).
- The trader pauses and emits a `factor data stale:` alert if the newest factor
  timestamp is older than 2 days (`FACTOR_MAX_AGE_DAYS`, overridable via env).
  The status file is at `runs/testnet/<testnet_id>/testnet_status.json` — check `live.status` and `alerts`.

## When it breaks

- A stage failure exits non-zero and leaves the previous parquet intact; the
  trader keeps using it until it crosses 2 days old, then pauses. Check
  `research/refresh_factors.log` for the failing stage.
- Common causes: OKX / CoinGecko / Bybit fetch errors, expired venv, VPS clock
  skew. Fix the cause and re-run `scripts/refresh_factors.sh` manually; the
  trader auto-resumes once factors are fresh again.
