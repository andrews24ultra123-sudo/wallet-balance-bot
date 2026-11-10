# Wallet Balance Bot

Read-only Telegram bot that watches public wallet addresses (native BTC, ETH, SOL, XRP, LTC and TRX; USDT and USDC on Ethereum; USDT on Tron) and alerts when a balance drops below its threshold. Each balance is shown with its chain, so the same asset on two chains appears as two rows.

What it does:
- Checks every wallet every 30 minutes (configurable) using free public block explorer APIs. No API keys, no Fireblocks access, nothing that can move funds.
- While a balance is below threshold it re-sends the LOW alert every scan, so it keeps pinging until topped up. On a healthy scan it posts a brief "all above threshold" confirmation, which is also how a recovery shows up.
- Runs a second, faster check every `alert_check_minutes` (default 16) in between the main scans. This one stays silent when everything is healthy and only sends the LOW alert if a balance is below threshold, so a low hot wallet is caught faster than once a cycle without adding noise.
- `/balances` scans all wallets on demand.
- If it cannot read a balance twice in a row, it tells you monitoring is degraded rather than failing silently.
- Optional daily heartbeat message so you know the bot itself is alive.

Commands (the bot only answers the configured chat id):
- `/balances` - scan all wallets now
- `/maxtopup` - amounts to bring every wallet up to target (calculation only; does not move funds)
- `/thresholds` - show thresholds (with targets)
- `/targets` - show targets
- `/setthreshold <asset or label> <amount>` - change a threshold (persisted to config.yaml)
- `/settarget <asset or label> <amount>` - change a target (persisted to config.yaml)
- `/setalertcheck <minutes>` - change the fast-check interval (persisted to config.yaml, takes effect immediately)
- `/help`

For an asset on more than one chain, add the chain to pick one row: `/setthreshold USDT TRX 2000` (also `USDT-TRX`, `USDT (TRX)` or `USDT tron`). `USDT` alone matches both rows and the bot asks which.

Chains: each wallet entry can carry `chain:` (BTC, ETH, SOL, XRP, LTC, TRX, or the full name). Left out, USDT and USDC mean Ethereum, so configs written before chains existed work unchanged. Tron addresses are checked when the config loads, so a typo stops the bot with a clear error. The stuck-withdrawal watch covers Ethereum wallets only.

XRP note: balances are reported as spendable, i.e. total minus the XRPL reserve (1 XRP base + 0.2 per owned object), because the reserve cannot fund withdrawals.

TRX note: the TRX balance is liquid TRX only; any staked TRX is excluded. TRX also pays the network fee for USDT-on-Tron withdrawals, so set the TRX threshold with those fees in mind. Tron balances come from TronGrid, with PublicNode as the fallback, both without an API key.

## Setup

### 1. Create the Telegram bot
1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts. Copy the token.
2. Message your new bot anything (this registers your chat).
3. Get your chat id:
   ```
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Your numeric chat id is at `result[0].message.chat.id`.

### 2. Configure
```
cp config.example.yaml config.yaml
```
Fill in `allowed_chat_id`, the wallet addresses and thresholds. Paste the bot token into a `.env` file next to `config.yaml` (`TELEGRAM_BOT_TOKEN=...`); the bot reads it automatically. On the server the systemd unit's `/etc/wallet-bot.env` takes precedence.

### 3. Test locally
```
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python -m bot.main --check          # one-shot balance scan, no Telegram needed
TELEGRAM_BOT_TOKEN=... venv/bin/python -m bot.main   # run the actual bot
```
To see the alert flow, temporarily set a threshold above the real balance and watch for the LOW alert, then `/setthreshold` it back down and wait for the next "all above threshold" confirmation.

## Deploy (Hostinger VPS)

Needs a VPS plan (KVM 1 is plenty, Ubuntu 24.04). Shared web hosting cannot run a persistent process.

```bash
# as root on the VPS
apt update && apt install -y python3-venv git
useradd --system --create-home walletbot

git clone https://github.com/<you>/wallet-balance-bot.git /opt/wallet-balance-bot
cd /opt/wallet-balance-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml
nano config.yaml                      # chat id, addresses, thresholds

echo 'TELEGRAM_BOT_TOKEN=<token>' > /etc/wallet-bot.env
chmod 600 /etc/wallet-bot.env

chown -R walletbot:walletbot /opt/wallet-balance-bot
cp deploy/wallet-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wallet-bot
journalctl -u wallet-bot -f           # watch it come up; expect "bot online" in Telegram
```

### Updating
Edit locally, run the tests, push to GitHub, then on the VPS as root (hPanel > VPS > Browser terminal opens at `root@<host>:~#`). The running bot is untouched until the restart in step 5.

```bash
# 1. Go to the bot and note the current commit: this is your rollback point
cd /opt/wallet-balance-bot
git log --oneline -1

# 2. Back up the files git does not hold (time-stamped, so two updates in a day never overwrite each other)
STAMP=$(date +%Y%m%d-%H%M); cp config.yaml config.yaml.bak-$STAMP; cp state.json state.json.bak-$STAMP; echo $STAMP

# 3. Pull the new code (expect "Fast-forward")
git pull

# 3a. Only if requirements.txt is in the pull's file list
venv/bin/pip install -r requirements.txt

# 4. Read-only test of the new code against the live config (stop on "Config error" or "[FAIL]")
venv/bin/python -m bot.main --config config.yaml --check

# 5. Go live and confirm
systemctl restart wallet-bot
sleep 10; systemctl is-active wallet-bot
journalctl -u wallet-bot --since "5 minutes ago" --no-pager | grep -E "Starting bot|Error|Traceback"
```
Expect `active`, one `Starting bot: N wallets` line and no `Error` or `Traceback`. Then in Telegram: "Wallet balance bot online", then `/balances`.

If an update also needs a config change, make it after step 3 and before step 4, so the `--check` tests the code and the config together. Do not use `/setthreshold` or `/settarget` between steps 3 and 5: they rewrite `config.yaml`.

Rollback (puts the old code and config back and keeps the current config aside; use the commit from step 1 and the stamp from step 2):
```bash
cd /opt/wallet-balance-bot && cp config.yaml config.yaml.rolledback && git checkout <commit> && cp config.yaml.bak-<stamp> config.yaml && systemctl restart wallet-bot
```
A rollback leaves a detached checkout, so a plain `git pull` no longer works. Come forward again with:
```bash
cd /opt/wallet-balance-bot && git checkout main && git pull && cp config.yaml.rolledback config.yaml && systemctl restart wallet-bot
```

### Day-to-day
- `systemctl status wallet-bot` - is it running
- `journalctl -u wallet-bot --since today` - today's logs
- Thresholds: change via `/setthreshold` in Telegram (persists on the VPS)
- Add or remove a wallet: edit `config.yaml` on the VPS, then `systemctl restart wallet-bot`
