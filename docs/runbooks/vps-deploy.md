# Runbook — VPS Deploy (ETH paper dry-run)

Deploy the dashboard + a **paper** trader (mainnet data, virtual fills) +
daily factor refresh on an always-on Linux VPS so `eth_s5_half_size`
dry-runs continuously and its win rate can be watched in the browser.

The trader runs as its **own** Docker stack, independent of the dashboard, so
redeploying the dashboard never kills a running dry-run. Two compose files:

| Stack     | File                        | Compose project | Rebuild when |
|-----------|-----------------------------|-----------------|--------------|
| dashboard | `docker-compose.yml`        | `dashboard`     | server/web code changes |
| trader    | `docker-compose.trader.yml` | `vibe-trader`   | **only** trader code changes |

Assumes Ubuntu 22.04+ and a non-root sudo user. Replace `REPO` with the repo
path you clone to, e.g. `/home/eric/Vibe-Trading`.

---

## 0. Prereqs (one-time)

```bash
# Docker engine + compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # then log out/in so docker works rootless
docker compose version               # confirm the compose plugin is present

# Git + python for the research venv (Ubuntu 24.04 ships python3.12, which
# satisfies the project's requires-python >=3.11 — no python3.11 PPA needed)
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip
```

## 1. Clone the repo

```bash
git clone <your-repo-url> REPO
cd REPO
git checkout quant-trading-dashboard     # branch with the paper-mode + decoupled trader
```

## 2. Trading mode + credentials

Paper mode is the default and **needs no API keys** — it reads real mainnet
prices over public endpoints and simulates fills locally. Keys are only for
`testnet` / `live`.

```bash
cp dashboard/.env.example dashboard/.env
# Defaults are fine for paper:
#   TRADING_MODE=paper
#   PAPER_EQUITY=10000   PAPER_TAKER_FEE=0.00055   PAPER_SLIPPAGE_BPS=5
# Leave BYBIT_API_KEY / BYBIT_API_SECRET blank for paper.
```

`dashboard/.env` is gitignored — never commit it. Both compose files read it
(run compose commands from `REPO/dashboard`). For `testnet`/`live` set
`TRADING_MODE` accordingly and fill the keys.

**Kill-switch thresholds (required edit for `eth_s5`).** The trader auto-pauses
at 5% drawdown and auto-terminates at 7% by default. `eth_s5_half_size`'s OOS
max drawdown is ≈9%, so with the defaults it **will** self-terminate mid-run.
Raise the thresholds above the strategy's expected DD in `dashboard/.env`:

```bash
# in dashboard/.env — must exceed eth_s5's ~9% OOS drawdown
KILL_PAUSE_DD=0.08
KILL_TERMINATE_DD=0.12
```

These are read by the trader stack, so apply them **before** bringing up the
trader (step 7); changing them later needs a trader rebuild (see Operations).
On terminate the loop flips its own `control.json` to `stopped`, so the manager
will not respawn it into an immediate re-terminate loop — but you then have to
restart it deliberately, so size the thresholds to avoid nuisance stops.

## 3. Research venv (for the factor refresh)

```bash
cd REPO
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .          # installs pandas/scipy/numpy/ccxt/requests/jinja2/...
pip install pyarrow       # parquet IO for factor_values_*.parquet
python -c "import pandas, ccxt, pyarrow, scipy; print('deps ok')"
deactivate
```

## 4. Seed factors + regime once (before first trade)

`refresh_factors.sh` runs stage0a + stage1 + **stage2.5 regime**, so it produces
both the factor parquet and `regime_<sym>.json`. The trader pauses if factors
are >2 days old; regime-overlay strategies (eth_s5) also need a current regime
file — stale regime silently decays the overlay (its alpha). Generate fresh now:

```bash
cd REPO
RESEARCH_VENV="REPO/.venv" bash scripts/refresh_factors.sh
# Confirm it ends with: refresh_factors: OK
python - <<'PY'
import json, datetime as dt
m = json.load(open("research/manifests/factor_values_eth.meta.json"))
print("eth factor index_end:", m["index_end"])           # ~today UTC
r = json.load(open("research/manifests/regime_eth.json"))
print("eth regime last bar:", r["breakdown"][-1]["date"]) # ~yesterday/today UTC
print("eth current_regime:", r["current_regime"])
PY
```

## 5. Daily refresh cron

```bash
crontab -e
```

Add this line (runs 00:30 UTC daily; absolute paths required in cron):

```cron
30 0 * * *  RESEARCH_VENV=REPO/.venv REPO/scripts/refresh_factors.sh >> REPO/research/refresh_factors.log 2>&1
```

## 6. Bring up the dashboard (server + web)

```bash
cd REPO/dashboard
docker compose up -d --build
docker compose ps        # server + web should be Up
```

The web UI is served on port 80. To reach it, either open the VPS firewall for
port 80, or (safer) SSH-tunnel from your laptop:

```bash
# from your laptop
ssh -L 8080:localhost:80 user@vps-ip
# then browse http://localhost:8080
```

## 7. Bring up the trader (separate stack)

```bash
cd REPO/dashboard
docker compose -f docker-compose.trader.yml up -d --build
docker compose -f docker-compose.trader.yml ps     # trader (manager) should be Up
```

The trader container runs the **manager**, which watches
`runs/testnet/<id>/control.json` and spawns a `trader.loop` per running entry.
It sits idle until you start a dry-run (next step).

## 8. Start a dry-run

Use the **UI button** — it is the primary path. Pressing it and the API call
below are identical: both hit `POST /api/testnet/<id>/start`, which writes the
same `control.json` that the manager reconciles into a running loop.

A strategy must be **promoted** first.

**UI (recommended):**

1. Open the dashboard → *實盤監控* page.
2. If the strategy is not promoted yet, promote it via its Promote dialog
   (non-fatal gates need an override reason).
3. *啟動新的 dry-run（paper）* → *新增 run* → pick the strategy, fill `run_dir`
   + symbol, mode `paper` → *啟動*.

The server writes `control.json` (desired_state=running); the manager picks it
up within ~5 s and launches the loop. The new run appears on the page.

<details>
<summary><b>Headless alternative — same effect via the API (no browser)</b></summary>

Use this only when you have no browser/tunnel access or want to script it.
`testnet_id` convention is `<strategy>_<mode>`.

```bash
# Promote eth_s5 (non-fatal gate → override reason required)
python3 - <<'PY'
import urllib.request, json
body = {"override_reason": "paper dry-run"}
req = urllib.request.Request(
    "http://localhost/api/strategies/eth_s5_half_size/promote",
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
    method="POST")
print(urllib.request.urlopen(req).read().decode())
PY

# Start a paper run
python3 - <<'PY'
import urllib.request, json
body = {"strategy_id": "eth_s5_half_size",
        "run_dir": "runs/eth_s5_half_size_oos",
        "symbol": "ETH/USDT:USDT", "interval": "1H", "qty": 0.01,
        "mode": "paper"}
req = urllib.request.Request(
    "http://localhost/api/testnet/eth_s5_half_size_paper/start",
    data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
    method="POST")
print(urllib.request.urlopen(req).read().decode())
PY
```

</details>

## 9. Verify it is running

```bash
# Control state (server view)
curl -s "http://localhost/api/testnet/eth_s5_half_size_paper/process?strategy_id=eth_s5_half_size"
# → {"running": true, "desired_state": "running", "testnet_id": "..."}

# Manager actually spawned the loop?
docker compose -f docker-compose.trader.yml logs --tail=20
# → "Started trader eth_s5_half_size_paper (mode=paper)"

# Live status (after ~1 tick) — read the bind-mounted file directly
cat REPO/runs/testnet/eth_s5_half_size_paper/testnet_status.json
# Expect: mode "paper", live.status "running", equity ~PAPER_EQUITY, no
# "factor data stale" alert. trades may stay 0 until the eth_s5 entry fires.
```

After this the dry-run runs continuously; cron refreshes factors nightly;
virtual fills are recorded automatically when the signal fires.

---

## Operations

- **Logs:**
  - dashboard: `docker compose logs -f server`
  - trader: `docker compose -f docker-compose.trader.yml logs -f` (manager +
    every spawned loop's output)
- **Stop a run:**
  ```bash
  curl -s -X POST "http://localhost/api/testnet/eth_s5_half_size_paper/stop?strategy_id=eth_s5_half_size"
  ```
  Writes `desired_state=stopped`; the manager stops the loop within ~5 s. The
  paper account is preserved (see persistence below) — restarting resumes it.
- **Update DASHBOARD code (does NOT touch the dry-run):**
  ```bash
  cd REPO && git pull
  cd dashboard && docker compose up -d --build      # only server+web rebuilt
  ```
  The trader stack is a different compose project (`vibe-trader`), so it keeps
  running. This is the whole point of the split — deploy freely.
- **Update TRADER code (brief restart, state preserved):**
  ```bash
  cd REPO && git pull
  cd dashboard && docker compose -f docker-compose.trader.yml up -d --build
  ```
  The manager restarts, re-reads the control files, and resumes every running
  id. Paper account and drawdown peak persist (below), so no reset / no
  equity-curve jump.
- **Change kill-switch thresholds:** edit `KILL_PAUSE_DD` / `KILL_TERMINATE_DD`
  in `dashboard/.env`, then recreate the trader so the loop picks up the new env:
  ```bash
  cd REPO/dashboard && docker compose -f docker-compose.trader.yml up -d
  ```
  Drawdown peak persists (`killswitch_state.json`), so the new threshold is
  measured against the existing peak — no equity-curve reset.
- **Factor freshness / refresh failures:** see `docs/runbooks/factor-refresh.md`
  and `research/refresh_factors.log`.

## State persistence (survives restarts)

Everything for a run lives in `REPO/runs/testnet/<id>/` (bind-mounted into both
containers):

| File                    | Owner   | Purpose |
|-------------------------|---------|---------|
| `control.json`          | server  | desired_state (running/stopped) + launch params |
| `testnet_status.json`   | loop    | live status the dashboard polls |
| `equity.csv` / `trades.csv` | loop | curve + fills shown in the UI |
| `paper_state.json`      | loop    | virtual cash / position / funding cursor (paper) |
| `killswitch_state.json` | loop    | drawdown peak equity |

Because `paper_state.json` and `killswitch_state.json` are reloaded on launch, a
trader restart resumes the same virtual account and drawdown instead of
resetting to `PAPER_EQUITY`.

## Security notes

- **Paper mode uses mainnet DATA only** (public price/orderbook/funding) and
  places **no real orders** — no API keys, no funds at risk.
- `testnet` needs sandbox keys; `live` places **real orders** with real funds —
  only set `TRADING_MODE=live` deliberately and guard the keys.
- Keep `dashboard/.env` 600 / never commit it.
- Do not expose port 80 publicly without auth; prefer the SSH tunnel.
