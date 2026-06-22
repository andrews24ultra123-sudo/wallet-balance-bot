"""Wallet balance Telegram bot.

Read-only monitor: checks public addresses on a schedule and alerts the
configured Telegram chat when a balance drops below its threshold.

Usage:
    python -m bot.main --check            # one-shot scan, no Telegram needed
    python -m bot.main                    # run the bot (needs token + chat id)
"""

import argparse
import asyncio
import html as _html
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode

from . import fetchers
from .config import ConfigError, find_wallets, load_config, save_threshold

logger = logging.getLogger("wallet-bot")

STATE_FILE = "state.json"


def fmt_amount(value):
    """Format a Decimal for display: max 4 decimal places, trailing zeros stripped."""
    rounded = round(value, 4)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def short_addr(address):
    if len(address) <= 14:
        return address
    return f"{address[:6]}...{address[-4:]}"


def display_label(label):
    """Drop a redundant ' hot wallet' suffix for display; they are all hot wallets."""
    suffix = " hot wallet"
    if label.lower().endswith(suffix):
        return label[: -len(suffix)].strip() or label
    return label


def _topup_line(wallet, balance):
    if wallet.get("target"):
        topup = wallet["target"] - balance
        if topup > 0:
            asset = wallet["asset"]
            return f"Top up:    {fmt_amount(topup)} {asset}  (target {fmt_amount(wallet['target'])} {asset})"
    return None


def _tags_line(tags):
    return " ".join(tags) if tags else None


def build_low_alert(low_wallets, tags=None, show_amounts=True):
    """Combined alert for one or more wallets below threshold. Re-sent every
    scan while any wallet stays low, so it keeps pinging until topped up."""
    lines = ["<b>LOW BALANCE</b>", ""]
    for wallet, balance in low_wallets:
        asset = wallet["asset"]
        label = _html.escape(display_label(wallet["label"]))
        lines.append(f"{label}")
        if show_amounts:
            lines.append(f"Balance:   {fmt_amount(balance)} {asset}")
            lines.append(f"Threshold: {fmt_amount(wallet['threshold'])} {asset}")
            topup = _topup_line(wallet, balance)
            if topup:
                lines.append(topup)
        else:
            lines.append("Below threshold")
        lines.append("")
    tag_line = _tags_line(tags)
    if tag_line:
        lines.append(tag_line)
    return "\n".join(lines).rstrip()


def build_scan(results, title="Wallet balances", show_amounts=True):
    """results: list of (wallet, balance_or_None, error_or_None)"""
    lines = [f"<b>{_html.escape(title)}</b>", ""]
    topups = []
    for wallet, balance, error in results:
        asset = wallet["asset"]
        label = _html.escape(display_label(wallet["label"]))
        if error:
            lines.append(f"{label}: fetch failed")
            continue
        status = "LOW" if balance < wallet["threshold"] else "OK"
        if show_amounts:
            line = f"{label}:  {fmt_amount(balance)} {asset}"
            if wallet.get("target"):
                pct = balance / wallet["target"] * 100
                line += f"  {pct:.1f}% full"
            line += f"  [{status}]"
            lines.append(line)
            if status == "LOW":
                tu = _topup_line(wallet, balance)
                if tu:
                    topups.append(f"  {label}: {tu.split(':', 1)[1].strip()}")
        else:
            lines.append(f"{label}:  [{status}]")
    if topups and show_amounts:
        lines += ["", "<b>Top ups needed:</b>"] + topups
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistent state (fetch failure tracking and heartbeat date only)


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
    return state.setdefault(label, {"fetch_failures": 0, "degraded_alerted": False})


# ---------------------------------------------------------------------------
# One-shot check mode (no Telegram)


def run_check(cfg):
    failures = 0
    for wallet in cfg["wallets"]:
        try:
            balance = fetchers.get_balance(wallet["asset"], wallet["address"])
            asset = wallet["asset"]
            status = "LOW" if balance < wallet["threshold"] else "OK "
            line = f"[{status}] {wallet['label']}: {fmt_amount(balance)} {asset}  (threshold {fmt_amount(wallet['threshold'])})"
            tu = _topup_line(wallet, balance)
            if tu and status.strip() == "LOW":
                line += f"\n       {tu}"
            print(line)
        except fetchers.FetchError as exc:
            failures += 1
            print(f"[FAIL] {wallet['label']}: {exc}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Bot mode


class WalletBot:
    def __init__(self, cfg, state_path):
        self.cfg = cfg
        self.state_path = state_path
        self.state = load_state(state_path)
        self.chat_id = cfg["allowed_chat_id"]

    async def send(self, bot, text):
        for chat_id in self.cfg["allowed_chat_ids"]:
            await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
            )

    async def fetch(self, wallet):
        return await asyncio.to_thread(
            fetchers.get_balance, wallet["asset"], wallet["address"]
        )

    async def tick(self, context):
        bot = context.bot
        low = []  # every wallet currently below threshold this tick

        for wallet in self.cfg["wallets"]:
            ws = wallet_state(self.state, wallet["label"])
            try:
                balance = await self.fetch(wallet)
            except fetchers.FetchError as exc:
                ws["fetch_failures"] += 1
                logger.warning("Fetch failed (%s consecutive): %s", ws["fetch_failures"], exc)
                if ws["fetch_failures"] >= 2 and not ws["degraded_alerted"]:
                    ws["degraded_alerted"] = True
                    label = _html.escape(display_label(wallet["label"]))
                    await self.send(
                        bot,
                        f"<b>Monitoring degraded:</b> {label}\n\n"
                        f"Could not read balance after {ws['fetch_failures']} attempts. "
                        "Balance alerts for this wallet are unreliable until this recovers.",
                    )
                continue

            if ws["degraded_alerted"]:
                label = _html.escape(display_label(wallet["label"]))
                await self.send(bot, f"<b>Monitoring restored:</b> {label}")
            ws["fetch_failures"] = 0
            ws["degraded_alerted"] = False

            if balance < wallet["threshold"]:
                low.append((wallet, balance))

        show_amounts = self.cfg["show_amounts"]
        tags = self.cfg.get("alert_tags") or []
        if low:
            # Re-sent every scan while anything stays low, so it keeps pinging
            # until the wallet is topped up back above threshold.
            await self.send(bot, build_low_alert(low, tags, show_amounts))
        elif not self.heartbeat_due():
            # When the daily heartbeat is due on this same tick it already
            # reports all clear, so skip the routine all-clear to avoid a duplicate.
            await self.send(bot, "<b>All wallets above threshold.</b>")

        await self.maybe_heartbeat(bot)
        save_state(self.state_path, self.state)

    def heartbeat_due(self):
        """True if the daily heartbeat should fire on this tick (not yet sent today)."""
        hb = self.cfg["heartbeat"]
        if not hb["enabled"]:
            return False
        tz = ZoneInfo(hb["timezone"])
        now = datetime.now(tz)
        hour, minute = (int(part) for part in str(hb["time"]).split(":"))
        due = now.hour > hour or (now.hour == hour and now.minute >= minute)
        already_sent = self.state.get("_last_heartbeat") == now.date().isoformat()
        return due and not already_sent

    async def maybe_heartbeat(self, bot):
        if not self.heartbeat_due():
            return
        now = datetime.now(ZoneInfo(self.cfg["heartbeat"]["timezone"]))
        self.state["_last_heartbeat"] = now.date().isoformat()
        date_str = now.strftime("%d %b %Y")
        await self.send(bot, await self.scan_text(title=f"Daily heartbeat: {date_str}"))

    async def scan_text(self, title="Wallet balances"):
        results = []
        for wallet in self.cfg["wallets"]:
            try:
                balance = await self.fetch(wallet)
                results.append((wallet, balance, None))
            except fetchers.FetchError as exc:
                results.append((wallet, None, exc))
        return build_scan(results, title=title, show_amounts=self.cfg["show_amounts"])

    # -- command handlers ---------------------------------------------------

    async def cmd_balances(self, update, context):
        await update.message.reply_text("Scanning...", parse_mode=ParseMode.HTML)
        await update.message.reply_text(
            await self.scan_text(), parse_mode=ParseMode.HTML
        )

    async def cmd_thresholds(self, update, context):
        lines = ["<b>Thresholds</b>", ""]
        for w in self.cfg["wallets"]:
            asset = w["asset"]
            label = _html.escape(display_label(w["label"]))
            target = f"  →  target {fmt_amount(w['target'])} {asset}" if w.get("target") else ""
            lines.append(f"{label}:  {fmt_amount(w['threshold'])} {asset}{target}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_setthreshold(self, update, context):
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /setthreshold &lt;asset or label&gt; &lt;amount&gt;\n"
                "Example: /setthreshold ETH 24",
                parse_mode=ParseMode.HTML,
            )
            return
        key, raw_amount = " ".join(args[:-1]), args[-1]
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation:
            await update.message.reply_text(f"'{_html.escape(raw_amount)}' is not a number.", parse_mode=ParseMode.HTML)
            return
        matches = find_wallets(self.cfg, key)
        if not matches:
            await update.message.reply_text(f"No wallet matches '{_html.escape(key)}'. See /thresholds.", parse_mode=ParseMode.HTML)
            return
        if len(matches) > 1:
            labels = ", ".join(_html.escape(display_label(w["label"])) for w in matches)
            await update.message.reply_text(
                f"'{_html.escape(key)}' matches several wallets ({labels}). Use the exact label.",
                parse_mode=ParseMode.HTML,
            )
            return
        wallet = matches[0]
        save_threshold(self.cfg, wallet["label"], amount)
        asset = wallet["asset"]
        label = _html.escape(display_label(wallet["label"]))
        await update.message.reply_text(
            f"Threshold updated: {label}\n"
            f"New threshold: {fmt_amount(amount)} {asset}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_help(self, update, context):
        check = self.cfg["intervals"]["check_minutes"]
        await update.message.reply_text(
            "<b>Wallet balance bot</b>\n\n"
            "/balances - scan all wallets now\n"
            "/thresholds - show thresholds and targets\n"
            "/setthreshold &lt;asset or label&gt; &lt;amount&gt; - change a threshold\n"
            "/help - this message\n\n"
            f"Scans every {check} min. Pings with @tags every scan while any balance is below "
            "threshold, otherwise a brief all-clear when everything is above threshold. "
            "A daily heartbeat proves the bot is alive.",
            parse_mode=ParseMode.HTML,
        )

    async def on_startup(self, app):
        await self.send(app.bot, "<b>Wallet balance bot online.</b> /help for commands.")


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

    only_andrew = filters.Chat(chat_id=cfg["allowed_chat_ids"])
    app.add_handler(CommandHandler("balances", bot.cmd_balances, filters=only_andrew))
    app.add_handler(CommandHandler("thresholds", bot.cmd_thresholds, filters=only_andrew))
    app.add_handler(CommandHandler("setthreshold", bot.cmd_setthreshold, filters=only_andrew))
    app.add_handler(CommandHandler("help", bot.cmd_help, filters=only_andrew))
    app.add_handler(CommandHandler("start", bot.cmd_help, filters=only_andrew))

    # Scan on a fixed interval, aligned to the next check-interval boundary.
    interval_s = cfg["intervals"]["check_minutes"] * 60
    now = datetime.now(timezone.utc)
    seconds_into_hour = now.minute * 60 + now.second + now.microsecond / 1_000_000
    first_delay = interval_s - (seconds_into_hour % interval_s)
    app.job_queue.run_repeating(bot.tick, interval=interval_s, first=first_delay, name="scan")

    logger.info(
        "Starting bot: %d wallets, scan every %d min, chat %s",
        len(cfg["wallets"]),
        cfg["intervals"]["check_minutes"],
        cfg["allowed_chat_id"],
    )
    app.run_polling(allowed_updates=["message"])


def load_env_file(path):
    """Load KEY=VALUE lines from a .env file next to the config, if present."""
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
