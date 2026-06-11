"""Wallet balance Telegram bot.

Read-only monitor: checks public addresses on a schedule and alerts the
configured Telegram chat when a balance drops below its threshold.

Usage:
    python -m bot.main --check            # one-shot scan, no Telegram needed
    python -m bot.main                    # run the bot (needs token + chat id)
"""

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from . import fetchers
from .config import ConfigError, find_wallets, load_config, save_threshold

logger = logging.getLogger("wallet-bot")

STATE_FILE = "state.json"


def fmt_amount(value):
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def short_addr(address):
    if len(address) <= 14:
        return address
    return f"{address[:7]}...{address[-5:]}"


def wallet_line(wallet, balance, show_amounts):
    status = "LOW" if balance < wallet["threshold"] else "OK"
    if show_amounts:
        return (
            f"[{status}] {wallet['label']} ({wallet['asset']} {short_addr(wallet['address'])}): "
            f"{fmt_amount(balance)} {wallet['asset']} (threshold {fmt_amount(wallet['threshold'])})"
        )
    return f"[{status}] {wallet['label']} ({wallet['asset']} {short_addr(wallet['address'])})"


# ---------------------------------------------------------------------------
# Persistent alert state


def load_state(path):
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def wallet_state(state, label):
    return state.setdefault(
        label, {"status": "OK", "fetch_failures": 0, "degraded_alerted": False}
    )


# ---------------------------------------------------------------------------
# One-shot check mode (no Telegram)


def run_check(cfg):
    failures = 0
    for wallet in cfg["wallets"]:
        try:
            balance = fetchers.get_balance(wallet["asset"], wallet["address"])
            print(wallet_line(wallet, balance, show_amounts=True))
        except fetchers.FetchError as exc:
            failures += 1
            print(f"[FETCH FAILED] {wallet['label']}: {exc}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Bot mode


class WalletBot:
    def __init__(self, cfg, state_path):
        self.cfg = cfg
        self.state_path = state_path
        self.state = load_state(state_path)
        self.tick_count = 0
        self.chat_id = cfg["allowed_chat_id"]

    async def send(self, bot, text):
        await bot.send_message(chat_id=self.chat_id, text=text)

    async def fetch(self, wallet):
        return await asyncio.to_thread(
            fetchers.get_balance, wallet["asset"], wallet["address"]
        )

    async def check_wallet(self, bot, wallet):
        ws = wallet_state(self.state, wallet["label"])
        show = self.cfg["show_amounts"]
        try:
            balance = await self.fetch(wallet)
        except fetchers.FetchError as exc:
            ws["fetch_failures"] += 1
            logger.warning("Fetch failed (%s consecutive): %s", ws["fetch_failures"], exc)
            if ws["fetch_failures"] >= 2 and not ws["degraded_alerted"]:
                ws["degraded_alerted"] = True
                await self.send(
                    bot,
                    f"MONITORING DEGRADED: cannot read balance for {wallet['label']} "
                    f"({wallet['asset']}) after {ws['fetch_failures']} consecutive attempts. "
                    "Balance alerts for this wallet are unreliable until this recovers.",
                )
            return

        if ws["degraded_alerted"]:
            await self.send(
                bot, f"Monitoring recovered for {wallet['label']} ({wallet['asset']})."
            )
        ws["fetch_failures"] = 0
        ws["degraded_alerted"] = False

        below = balance < wallet["threshold"]
        line = wallet_line(wallet, balance, show)
        reminder_min = self.cfg["intervals"]["reminder_minutes"]

        if below and ws["status"] == "OK":
            ws["status"] = "LOW"
            await self.send(
                bot,
                "LOW BALANCE ALERT\n"
                f"{line}\n"
                f"Top-up needed. Reminders every {reminder_min} minutes until it is back above threshold.",
            )
        elif below and ws["status"] == "LOW":
            await self.send(bot, f"Reminder, still low:\n{line}")
        elif not below and ws["status"] == "LOW":
            ws["status"] = "OK"
            await self.send(bot, f"RECOVERED, reminders stopped:\n{line}")

    async def tick(self, context):
        intervals = self.cfg["intervals"]
        ticks_per_full = max(1, intervals["check_minutes"] // intervals["reminder_minutes"])
        full_scan = self.tick_count % ticks_per_full == 0
        self.tick_count += 1

        for wallet in self.cfg["wallets"]:
            ws = wallet_state(self.state, wallet["label"])
            needs_attention = ws["status"] == "LOW" or ws["fetch_failures"] > 0
            if full_scan or needs_attention:
                await self.check_wallet(context.bot, wallet)

        await self.maybe_heartbeat(context.bot)
        save_state(self.state_path, self.state)

    async def maybe_heartbeat(self, bot):
        hb = self.cfg["heartbeat"]
        if not hb["enabled"]:
            return
        tz = ZoneInfo(hb["timezone"])
        now = datetime.now(tz)
        hour, minute = (int(part) for part in str(hb["time"]).split(":"))
        due = now.hour > hour or (now.hour == hour and now.minute >= minute)
        already_sent = self.state.get("_last_heartbeat") == now.date().isoformat()
        if due and not already_sent:
            self.state["_last_heartbeat"] = now.date().isoformat()
            await self.send(bot, "Daily heartbeat.\n" + await self.scan_text())

    async def scan_text(self):
        lines = []
        for wallet in self.cfg["wallets"]:
            try:
                balance = await self.fetch(wallet)
                lines.append(wallet_line(wallet, balance, self.cfg["show_amounts"]))
            except fetchers.FetchError:
                lines.append(f"[FETCH FAILED] {wallet['label']} ({wallet['asset']})")
        return "\n".join(lines)

    # -- command handlers ---------------------------------------------------

    async def cmd_balances(self, update, context):
        await update.message.reply_text("Scanning...")
        await update.message.reply_text(await self.scan_text())

    async def cmd_thresholds(self, update, context):
        lines = [
            f"{w['label']} ({w['asset']}): {fmt_amount(w['threshold'])} {w['asset']}"
            for w in self.cfg["wallets"]
        ]
        await update.message.reply_text("Current thresholds:\n" + "\n".join(lines))

    async def cmd_setthreshold(self, update, context):
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /setthreshold <asset or label> <amount>\n"
                "Example: /setthreshold ETH 0.5"
            )
            return
        key, raw_amount = " ".join(args[:-1]), args[-1]
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation:
            await update.message.reply_text(f"'{raw_amount}' is not a number.")
            return
        matches = find_wallets(self.cfg, key)
        if not matches:
            await update.message.reply_text(f"No wallet matches '{key}'. See /thresholds.")
            return
        if len(matches) > 1:
            labels = ", ".join(w["label"] for w in matches)
            await update.message.reply_text(
                f"'{key}' matches several wallets ({labels}). Use the exact label."
            )
            return
        wallet = matches[0]
        save_threshold(self.cfg, wallet["label"], amount)
        await update.message.reply_text(
            f"Threshold for {wallet['label']} ({wallet['asset']}) set to "
            f"{fmt_amount(amount)} {wallet['asset']}. Takes effect from the next check."
        )

    async def cmd_help(self, update, context):
        intervals = self.cfg["intervals"]
        await update.message.reply_text(
            "Wallet balance bot (read-only monitoring).\n\n"
            "/balances - scan all wallets now\n"
            "/thresholds - show current thresholds\n"
            "/setthreshold <asset or label> <amount> - change a threshold\n"
            "/help - this message\n\n"
            f"Automatic checks every {intervals['check_minutes']} min; reminders every "
            f"{intervals['reminder_minutes']} min while a balance is low."
        )

    async def on_startup(self, app):
        await self.send(app.bot, "Wallet balance bot online. /help for commands.")


def run_bot(cfg, state_path):
    # Imported here so --check mode works without python-telegram-bot installed.
    from telegram.ext import Application, CommandHandler, filters

    if not cfg["telegram_token"]:
        raise ConfigError("No Telegram token. Set TELEGRAM_BOT_TOKEN or telegram.token in config.yaml.")
    if not cfg["allowed_chat_id"]:
        raise ConfigError("telegram.allowed_chat_id missing from config.yaml.")

    bot = WalletBot(cfg, state_path)
    app = (
        Application.builder()
        .token(cfg["telegram_token"])
        .post_init(bot.on_startup)
        .build()
    )

    only_andrew = filters.Chat(chat_id=cfg["allowed_chat_id"])
    app.add_handler(CommandHandler("balances", bot.cmd_balances, filters=only_andrew))
    app.add_handler(CommandHandler("thresholds", bot.cmd_thresholds, filters=only_andrew))
    app.add_handler(CommandHandler("setthreshold", bot.cmd_setthreshold, filters=only_andrew))
    app.add_handler(CommandHandler("help", bot.cmd_help, filters=only_andrew))
    app.add_handler(CommandHandler("start", bot.cmd_help, filters=only_andrew))

    app.job_queue.run_repeating(
        bot.tick,
        interval=cfg["intervals"]["reminder_minutes"] * 60,
        first=5,
    )

    logger.info("Starting bot: %d wallets, chat %s", len(cfg["wallets"]), cfg["allowed_chat_id"])
    app.run_polling(allowed_updates=["message"])


def load_env_file(path):
    """Load KEY=VALUE lines from a .env file next to the config, if present.

    Existing environment variables win, so the systemd EnvironmentFile still
    takes precedence on the server.
    """
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main():
    parser = argparse.ArgumentParser(description="Wallet balance Telegram bot")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--state", default=STATE_FILE, help="path to state file")
    parser.add_argument(
        "--check", action="store_true", help="scan balances once, print, and exit"
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    load_env_file(os.path.join(os.path.dirname(os.path.abspath(args.config)), ".env"))
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        raise SystemExit(f"Config error: {exc}")

    if args.check:
        raise SystemExit(run_check(cfg))
    try:
        run_bot(cfg, args.state)
    except ConfigError as exc:
        raise SystemExit(f"Config error: {exc}")


if __name__ == "__main__":
    main()
