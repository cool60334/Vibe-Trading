#!/usr/bin/env bash
# One-shot Ubuntu 22.04 VPS installer for BTC paper-trade.
#
# Usage on fresh VPS:
#   ssh user@vps "bash -s" < install.sh
# or:
#   scp install.sh user@vps:~ && ssh user@vps && bash install.sh
#
# Idempotent — safe to re-run.

set -euo pipefail

REPO_URL="https://github.com/cool60334/Vibe-Trading.git"
BRANCH="my-research"
INSTALL_DIR="${INSTALL_DIR:-$HOME/Vibe-Trading}"

echo "[install] target: $INSTALL_DIR"

# ---------------- system packages ----------------
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git ca-certificates

# ---------------- repo ----------------
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "[install] repo exists, pulling latest"
  cd "$INSTALL_DIR"
  git fetch origin
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
else
  echo "[install] cloning repo"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ---------------- python venv ----------------
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install ccxt pandas numpy scipy requests pyarrow

# vibe-trading + backtest packages: install from PyPI mirror if available, else skip
pip install vibe-trading-ai 2>/dev/null || echo "[install] vibe-trading-ai not on PyPI, OK for paper-trade"

# ---------------- env file ----------------
if [ ! -f ".env" ]; then
  echo "[install] creating .env template"
  cat > .env <<'EOF'
# Bybit testnet API keys (get from https://testnet.bybit.com → API Management)
BYBIT_TESTNET_API_KEY=
BYBIT_TESTNET_API_SECRET=

# Live config
LIVE_CAPITAL_USDT=1000
LIVE_LEVERAGE=1.5
LIVE_DRY_RUN=0
EOF
  chmod 600 .env
  echo ""
  echo "==============================================="
  echo "  NEXT: edit $INSTALL_DIR/.env with your Bybit testnet API keys"
  echo "==============================================="
fi

# ---------------- systemd units ----------------
sudo mkdir -p /etc/systemd/system

SERVICE_PATH=/etc/systemd/system/btc-paper-trade.service
TIMER_PATH=/etc/systemd/system/btc-paper-trade.timer

USER_NAME="$(whoami)"
PYTHON_BIN="$INSTALL_DIR/.venv/bin/python"

sudo tee "$SERVICE_PATH" > /dev/null <<EOF
[Unit]
Description=BTC paper-trade (Bybit testnet) — hourly signal + reconcile
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$PYTHON_BIN $INSTALL_DIR/live/paper_trade.py
StandardOutput=append:$INSTALL_DIR/live/state/paper_trade.log
StandardError=append:$INSTALL_DIR/live/state/paper_trade.log
TimeoutStartSec=300
EOF

sudo tee "$TIMER_PATH" > /dev/null <<EOF
[Unit]
Description=Hourly trigger for BTC paper-trade

[Timer]
OnCalendar=hourly
Persistent=true
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable btc-paper-trade.timer
sudo systemctl start btc-paper-trade.timer

# Ensure state dir exists with right ownership
mkdir -p "$INSTALL_DIR/live/state"

echo ""
echo "[install] DONE"
echo ""
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/.env with Bybit testnet keys"
echo "  2. Test once:   $PYTHON_BIN $INSTALL_DIR/live/paper_trade.py --dry-run"
echo "  3. Watch timer: systemctl status btc-paper-trade.timer"
echo "  4. View log:    tail -f $INSTALL_DIR/live/state/paper_trade.log"
echo "  5. List runs:   systemctl list-timers | grep btc-paper-trade"
