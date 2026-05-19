# VPS + Bybit Testnet Deployment

End-to-end guide：開 VPS → Bybit testnet 申請 → 部署 → 監控。

---

## 0. 前置（在自己電腦先做）

1. 本機 `python live/alert_only.py` 跑通（已完成）
2. 累積 ≥ 7 天 alert-only log + `python live/check_parity.py --days 7` 通過 (match ≥ 95%)
3. 通過才往下走（沒通過代表 signal pipeline 有 bug，VPS 上跑只是放大問題）

---

## 1. Bybit Testnet API Key 申請

1. 開 https://testnet.bybit.com 註冊（用 throwaway email 即可）
2. 登入 → 右上 profile → **API Management**
3. **Create New Key**:
   - Name: `btc-paper-trade`
   - Type: **System-generated API Keys**
   - Permissions: **Contract → Orders, Positions**（讀 + 交易）
   - IP restriction: 等開好 VPS 後填 VPS IP（先空也可，但建議鎖）
4. 抄下 `API Key` + `API Secret`（**Secret 只顯示一次**）
5. testnet 帳號自動有 ~$10,000 USDT 假錢，不需要充值

⚠️ **不要與 mainnet API key 混用**。Testnet/mainnet 是完全分開系統。

---

## 2. VPS 選擇

| Provider | 規格 | 月費 | 區域建議 |
|----------|------|------|---------|
| **Vultr** | 1 vCPU 1GB | $5 | Tokyo / Singapore |
| Hetzner | 2 vCPU 4GB | $5 | Falkenstein（歐洲，離 Bybit 機房遠 +200ms latency）|
| DigitalOcean | 1 vCPU 1GB | $6 | Singapore |
| AWS Lightsail | 1 vCPU 1GB | $5 | Tokyo (ap-northeast-1) |

選 Tokyo / Singapore — Bybit 撮合在亞洲，latency < 50ms。歐美 VPS latency 200-300ms，testnet 影響小但 live 累積就明顯。

OS：**Ubuntu 22.04 LTS**。

---

## 3. VPS 初始設定

開好 VPS 後 SSH 進去：

```bash
ssh root@<your-vps-ip>
```

### 3.1 建一般使用者（避免直接 root 跑）

```bash
adduser cool6
usermod -aG sudo cool6
mkdir -p /home/cool6/.ssh
cp ~/.ssh/authorized_keys /home/cool6/.ssh/
chown -R cool6:cool6 /home/cool6/.ssh
chmod 700 /home/cool6/.ssh
chmod 600 /home/cool6/.ssh/authorized_keys

# 禁止 root SSH 登入（強烈建議）
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart sshd

# 切到 cool6
su - cool6
```

### 3.2 防火牆（ufw）

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp     # SSH
sudo ufw enable
sudo ufw status
```

---

## 4. 部署

把 `install.sh` 推上 VPS 跑：

```bash
# 從本機推
scp live/deploy/install.sh cool6@<your-vps-ip>:~

# SSH 進去跑
ssh cool6@<your-vps-ip>
bash ~/install.sh
```

Script 會：
1. 安裝 Python + git
2. clone repo（`my-research` branch）到 `~/Vibe-Trading`
3. 建 venv + pip install 依賴
4. 寫 `.env` 模板（**待你填 API key**）
5. 建 `btc-paper-trade.service` + `.timer` systemd 單元
6. 啟動 timer（每小時整點觸發）

### 4.1 填 Bybit key

```bash
nano ~/Vibe-Trading/.env
```

填入剛才抄的 testnet API key + secret。`chmod 600 .env` 保護權限（install.sh 已做）。

### 4.2 Dry-run 驗證

```bash
cd ~/Vibe-Trading
.venv/bin/python live/paper_trade.py --dry-run
```

應看到：
```
[bybit] sandbox mode ON (testnet)
[bybit] testnet balance: 10000.00 USDT
[reconcile] target=+0.000 ...
[order] {"status": "no_op", "reason": "delta below min_order_btc", ...}
```

如果 status = `error`，看 error 訊息 → 多半 API key 沒設對 / 沒給 contract 權限。

### 4.3 開啟自動排程

確認 timer 已啟動：

```bash
systemctl status btc-paper-trade.timer
systemctl list-timers | grep btc-paper-trade
```

下次整點觸發 → 自動跑 `paper_trade.py`。

---

## 5. 監控

### 5.1 看 log

```bash
tail -f ~/Vibe-Trading/live/state/paper_trade.log
```

每小時應看到 fetch + signal + reconcile 區塊。

### 5.2 查訊號 + 訂單 log

```bash
cd ~/Vibe-Trading
.venv/bin/python -c "
import pandas as pd
sig = pd.read_parquet('live/state/signals.parquet')
ord = pd.read_parquet('live/state/orders.parquet') if __import__('pathlib').Path('live/state/orders.parquet').exists() else None
print('=== signals (last 12) ===')
print(sig.tail(12)[['regime','position','close','funding']])
if ord is not None:
    print('\n=== orders (last 10) ===')
    print(ord.tail(10))
"
```

### 5.3 看 Bybit testnet 倉位

登 https://testnet.bybit.com → Derivatives → USDT Perp → 看 BTCUSDT 倉位是否與 `target_btc` 對齊。

### 5.4 systemd 健康

```bash
systemctl status btc-paper-trade.service       # 上次跑結果
journalctl -u btc-paper-trade.service -n 100   # 完整 systemd log
```

---

## 6. 升級策略

需要拉新 commit 時：

```bash
cd ~/Vibe-Trading
git pull origin my-research
.venv/bin/pip install -r requirements.txt 2>/dev/null || true
sudo systemctl restart btc-paper-trade.timer
```

或直接重跑 `bash ~/install.sh`（idempotent）。

---

## 7. 安全注意

| 項 | 為何 |
|----|------|
| `.env` chmod 600 + 不進 git | API key 流出 = 別人能下單（即使 testnet 是假錢，習慣要養）|
| Bybit API key IP whitelist 鎖 VPS IP | 即使 secret 流出也只能從 VPS 用 |
| `BYBIT_LIVE` 環境變數不要設 | 防呆 — 永遠 testnet |
| `SafetyConfig.max_position_btc = 0.05` 硬編碼 | 即使 signal bug 也不會無上限開倉 |
| `mark_drift_abort_pct = 2%` | 訊號生成到下單期間市場急動 = 棄單 |
| `daily_order_cap = 24` | 防失控 loop 連續下單 |

如果未來要 mainnet 真錢：**必須** review 整份 `bybit_client.py` 的 SafetyConfig，並把 `max_position_btc` 改保守、加 stop-loss、加 alert（Telegram / email），且至少跑滿 30 天 testnet + ≥ 10 完整 trade 後才考慮。

---

## 8. 驗證標準（什麼時候算 testnet 跑成功）

按 `alpha-workflow.md` 階段 3 三層驗證：

| 層 | 標準 | 預期時間 |
|----|------|---------|
| L1 Infra（已做）| `check_parity.py` match ≥ 95% × 7 天 | 7 天 |
| **L2 Execution（這層）** | ≥ 5 完整 round-trip + slippage < 2× config + 倉位偏差 < 1% | 等 bear 觸發，1-6 月 |
| L3 Performance | ≥ 20 trades + live PnL 在 backtest expected ±30% | 完整 bear cycle |

L1 通過 → 開 testnet  
L2 通過 → 真錢小額 ($100-200)  
L3 通過 → 擴大本金

---

## 9. 排錯

| 症狀 | 處理 |
|------|------|
| `ERROR: BYBIT_TESTNET_API_KEY not set` | `.env` 沒填 / `EnvironmentFile=` 路徑錯 |
| `bybit { "retCode": 10003, ... }` | API key 無效，重申請 |
| `bybit { "retCode": 110007, ... }` | 餘額不足 — testnet 通常 $10k，沒事；mainnet 真的不夠錢 |
| `bybit { "retCode": 110045, ... }` | 倉位模式衝突（hedge vs one-way）— 在 testnet UI 切到 One-Way Mode |
| `mark drift X% > 2% threshold` | 市場急動 + 訊號滯後，下次重試（正常防呆）|
| Timer 沒跑 | `systemctl status btc-paper-trade.timer`、看 `journalctl` |
| `ccxt.NetworkError` | VPS DNS / 防火牆問題，`ping api-testnet.bybit.com` 驗 |
| log 在跑但訂單從不送 | 看 `[reconcile] target=` — 若一直 0 表示 regime=bull (gate 關)，正常 |
