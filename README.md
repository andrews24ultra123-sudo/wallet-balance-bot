# Wallet Balance Bot

Read-only Telegram bot that watches public wallet addresses (native BTC, ETH, SOL, XRP, LTC) and alerts when a balance drops below its threshold.

What it does:
- Checks every wallet every 30 minutes (configurable) using free public block explorer APIs. No API keys, no Fireblocks access, nothing that can move funds.
- When a balance drops below threshold: one alert, then a reminder every 10 minutes until the balance is back above threshold, then a one-off "recovered" message.
- `/balances` scans all wallets on demand.
- If it cannot read a balance twice in a row, it tells you monitoring is degraded rather than failing silently.
- Optional daily heartbeat message so you know the bot itself is alive.

Commands (the bot only answers the configured chat id):
- `/balances` - scan all wallets now
- `/thresholds` - show thresholds
- `/setthreshold <asset or label> <amount>` - change a threshold (persisted to config.yaml)
- `/help`

XRP note: balances are reported as spendable, i.e. total minus the XRPL reserve (1 XRP base + 0.2 per owned object), because the reserve cannot fund withdrawals.

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
Fill in `allowed_chat_id`, the wallet addresses and thresholds. For local testing you can put the token in `config.yaml`; on the server use the environment variable instead.

### 3. Test locally
```
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python -m bot.main --check          # one-shot balance scan, no Telegram needed
TELEGRAM_BOT_TOKEN=... venv/bin/python -m bot.main   # run the actual bot
```
To see the alert flow, temporarily set a threshold above the real balance and watch for the LOW alert, reminders, then `/setthreshold` it back down and wait for the recovery message.

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
Edit locally, push to GitHub, then on the VPS:
```bash
cd /opt/wallet-balance-bot && git pull && systemctl restart wallet-bot
```

### Day-to-day
- `systemctl status wallet-bot` - is it running
- `journalctl -u wallet-bot --since today` - today's logs
- Thresholds: change via `/setthreshold` in Telegram (persists on the VPS)
- Add or remove a wallet: edit `config.yaml` on the VPS, then `systemctl restart wallet-bot`
