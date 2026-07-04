# Fable 5 伺服器唯讀存取 — 設定手冊（已驗證）

> 目的：給開發期的 Fable 5 一個 **唯讀、無 sudo、讀不到機敏檔** 的 SSH 帳號，理解線上實況。
> 真錢交易伺服器 —— 全程唯讀，用完即收。

## 前提
- Fable 5 從**使用者自己控制的機器**跑 SSH（私鑰留該機，不外傳）。
- 伺服器為 Debian/Ubuntu 系，操作以 root。
- Repo 路徑範例 `/home/eric/REPO`（換成實際路徑）。

---

## 1. 客戶端：產專用金鑰（Windows PowerShell）
```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.ssh" | Out-Null
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\fable_ro" -C "fable-ro readonly"   # passphrase 留空按兩次 Enter
Get-Content "$env:USERPROFILE\.ssh\fable_ro.pub"   # 取公鑰，貼到步驟 2
```
- `fable_ro`（私鑰）留本機、別動、別貼任何 AI/聊天。
- Windows ssh-keygen 不展開 `~`，且 `.ssh` 需先存在 → 用完整路徑 + 先建資料夾。

## 2. 伺服器：建無 sudo、僅金鑰登入的帳號
```bash
adduser --disabled-password --gecos "" fable-ro
mkdir -p /home/fable-ro/.ssh && chmod 700 /home/fable-ro/.ssh
echo "<貼上 fable_ro.pub 內容>" > /home/fable-ro/.ssh/authorized_keys
chmod 600 /home/fable-ro/.ssh/authorized_keys
chown -R fable-ro:fable-ro /home/fable-ro/.ssh
```

## 3. ⚠️ 絕不把 fable-ro 加進 `docker` 群組
`docker` 群組 = 等同 root（可掛 host FS、逃逸容器）。
- 要 `docker ps` → 只加窄 sudoers：`echo 'fable-ro ALL=(root) NOPASSWD: /usr/bin/docker ps' > /etc/sudoers.d/fable-ro`
- 或不給 docker → Fable 5 改讀磁碟上的 log 檔 + `runs/testnet/<id>/*.json` 狀態檔。

## 4. ACL：授讀整個 REPO，擋機敏檔
```bash
apt-get update && apt-get install -y acl      # 若 getfacl 不存在

setfacl -m u:fable-ro:--x /home/eric          # 只穿越、不列 eric 家目錄
setfacl -R  -m u:fable-ro:rX /home/eric/REPO  # 遞迴授讀（rX 不給資料檔執行位）
setfacl -R -d -m u:fable-ro:rX /home/eric/REPO # 預設 ACL：新產生的 runs/manifests 也可讀

# 撤機敏檔讀權（順序須在上面之後；保留 .env.example 模板）
find /home/eric/REPO \( -name ".env" -o -name ".env.*" \) ! -name "*.example" \
  -exec setfacl -x u:fable-ro {} \; -exec chmod 600 {} \;
find /home/eric/REPO \( -name "*.pem" -o -name "*.key" -o -name "id_rsa" \
  -o -name ".git-credentials" \) -exec setfacl -x u:fable-ro {} \; -exec chmod 600 {} \;
```
**維護規則**：日後新增任何 `.env`/金鑰 → 補跑上面 `find ... setfacl -x` 一次（預設 ACL 會讓新檔預設可讀）。

## 5. 驗收（三個必對）
```bash
sudo -u fable-ro cat /home/eric/REPO/dashboard/.env                # 必 Permission denied
sudo -u fable-ro cat /home/eric/REPO/research/research_config.yaml # 必成功
sudo -u fable-ro ls  /home/eric/REPO/runs/pipeline_jobs            # 必成功
```

## 6. 交給 Fable 5 的連線字串
```
ssh -i ~/.ssh/fable_ro fable-ro@<server-ip>
```
（填進 `docs/talos/fable5-prompt.md` §1.5 連線資訊）

## 7. 收工（Fable 5 跑完 Part A/B 後務必做）
```bash
# 停用登入
passwd -l fable-ro
# 或徹底移除公鑰
: > /home/fable-ro/.ssh/authorized_keys
# 需要的話連 ACL 也撤
setfacl -R -x u:fable-ro /home/eric/REPO
setfacl -x u:fable-ro /home/eric
```

---
**狀態**：2026-07-04 設定完成並驗證通過。
