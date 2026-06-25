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
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode

from . import fetchers
from .config import ConfigError, find_wallets, load_config, save_target, save_threshold

logger = logging.getLogger("wallet-bot")

STATE_FILE = "state.json"


def fmt_amount(value):
    """Format a Decimal for display: 2 decimal places, rounded down so a balance
    is never overstated, with thousands separators (e.g. 49,141.92)."""
    q = value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return f"{q:,.2f}"


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


def pre_table(field_names, rows):
    """Borderless, column-aligned table in a <pre> block (monospace in Telegram)."""
    from prettytable import PrettyTable

    table = PrettyTable()
    table.border = False
    table.align = "l"
    table.field_names = field_names
    for row in rows:
        table.add_row(row)
    return f"<pre>{_html.escape(table.get_string())}</pre>"


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
    """results: list of (wallet, balance_or_None, error_or_None).

    Renders a borderless, column-aligned table inside a <pre> block (Telegram
    shows <pre> in a monospace font, so the columns line up). A status emoji is
    appended after each row, outside the aligned columns so it can't skew them.
    """
    from prettytable import PrettyTable

    table = PrettyTable()
    table.border = False
    table.align = "l"
    table.field_names = ["Asset", "Balance", "Full", "St"] if show_amounts else ["Asset", "St"]

    badges = []
    topups = []
    for wallet, balance, error in results:
        label = display_label(wallet["label"])  # raw; the whole table is escaped below
        if error:
            badges.append("⚠️")
            table.add_row([label, "fetch failed", "", "?"] if show_amounts else [label, "?"])
            continue
        status = "LOW" if balance < wallet["threshold"] else "OK"
        badges.append("✅" if status == "OK" else "❌")
        if show_amounts:
            full = f"{balance / wallet['target'] * 100:.1f}%" if wallet.get("target") else ""
            table.add_row([label, fmt_amount(balance), full, status])
            if status == "LOW":
                tu = _topup_line(wallet, balance)
                if tu:
                    topups.append(f"  {_html.escape(label)}: {tu.split(':', 1)[1].strip()}")
        else:
            table.add_row([label, status])

    # First output line is the header; data rows follow in insertion order.
    # Pad every row to a common width so the trailing emoji line up in a column.
    out_lines = table.get_string().split("\n")
    width = max(len(line.rstrip()) for line in out_lines)
    for i, badge in enumerate(badges):
        row = i + 1
        if row < len(out_lines):
            out_lines[row] = out_lines[row].rstrip().ljust(width) + "  " + badge
    table_str = _html.escape("\n".join(out_lines))

    lines = [f"<b>{_html.escape(title)}</b>", "", f"<pre>{table_str}</pre>"]
    if topups and show_amounts:
        lines += ["", "<b>Top ups needed:</b>"] + topups
    return "\n".join(lines)


def build_topup_plan(results):
    """Funding plan: amount needed to bring each wallet up to its target.

    Rendered as a borderless monospace table inside a <pre> block, matching the
    /balances layout. results: list of (wallet, balance_or_None, error_or_None).
    Calculation only; the bot does not move funds.
    """
    rows = []
    any_needed = False
    for wallet, balance, error in results:
        label = display_label(wallet["label"])
        if error:
            rows.append([label, "fetch failed", "", ""])
            continue
        target = wallet.get("target")
        if not target:
            rows.append([label, "no target", fmt_amount(balance), ""])
            continue
        topup = target - balance
        if topup > 0:
            any_needed = True
            rows.append([label, f"+{fmt_amount(topup)}", fmt_amount(balance), fmt_amount(target)])
        else:
            rows.append([label, "at target", fmt_amount(balance), fmt_amount(target)])

    lines = ["<b>Top-up to target</b>", "", pre_table(["Asset", "Top up", "Current", "Target"], rows)]
    if not any_needed:
        lines += ["", "All wallets are at or above target. Nothing to top up."]
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

    async def tick_alert(self, context):
        """Fast supplementary scan: silent unless a wallet is below threshold.

        Runs on the shorter `alert_check_minutes` cadence between the hourly
        `tick`s. Read-only: it does not mutate state, send an all-clear, run the
        heartbeat, or raise degraded/restored alerts (the hourly `tick` owns all
        of that), so it cannot race with `tick`. It only pings when something is
        low, to catch a low hot wallet faster than once an hour."""
        bot = context.bot
        low = []
        for wallet in self.cfg["wallets"]:
            try:
                balance = await self.fetch(wallet)
            except fetchers.FetchError as exc:
                logger.warning("Fast check fetch failed (%s): %s", wallet["label"], exc)
                continue
            if balance < wallet["threshold"]:
                low.append((wallet, balance))

        if low:
            show_amounts = self.cfg["show_amounts"]
            tags = self.cfg.get("alert_tags") or []
            await self.send(bot, build_low_alert(low, tags, show_amounts))

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

    async def fetch_all(self):
        """Fetch every wallet's balance: list of (wallet, balance_or_None, error_or_None)."""
        results = []
        for wallet in self.cfg["wallets"]:
            try:
                results.append((wallet, await self.fetch(wallet), None))
            except fetchers.FetchError as exc:
                results.append((wallet, None, exc))
        return results

    async def scan_text(self, title="Wallet balances"):
        return build_scan(
            await self.fetch_all(), title=title, show_amounts=self.cfg["show_amounts"]
        )

    # -- command handlers ---------------------------------------------------

    async def cmd_balances(self, update, context):
        await update.message.reply_text("Scanning...", parse_mode=ParseMode.HTML)
        await update.message.reply_text(
            await self.scan_text(), parse_mode=ParseMode.HTML
        )

    async def cmd_maxtopup(self, update, context):
        await update.message.reply_text("Calculating top-up to target...", parse_mode=ParseMode.HTML)
        await update.message.reply_text(
            build_topup_plan(await self.fetch_all()), parse_mode=ParseMode.HTML
        )

    async def cmd_thresholds(self, update, context):
        rows = [
            [
                display_label(w["label"]),
                fmt_amount(w["threshold"]),
                fmt_amount(w["target"]) if w.get("target") else "-",
            ]
            for w in self.cfg["wallets"]
        ]
        text = "<b>Thresholds</b>\n\n" + pre_table(["Asset", "Threshold", "Target"], rows)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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
        if amount <= 0:
            await update.message.reply_text("Threshold must be greater than zero.", parse_mode=ParseMode.HTML)
            return
        if wallet.get("target") and amount > wallet["target"]:
            await update.message.reply_text(
                f"Threshold {fmt_amount(amount)} {wallet['asset']} is above the target "
                f"({fmt_amount(wallet['target'])} {wallet['asset']}). The threshold is the alert "
                "line and should sit at or below the top-up target. Not changed.",
                parse_mode=ParseMode.HTML,
            )
            return
        save_threshold(self.cfg, wallet["label"], amount)
        asset = wallet["asset"]
        label = _html.escape(display_label(wallet["label"]))
        await update.message.reply_text(
            f"Threshold updated: {label}\n"
            f"New threshold: {fmt_amount(amount)} {asset}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_targets(self, update, context):
        rows = [
            [display_label(w["label"]), fmt_amount(w["target"]) if w.get("target") else "no target"]
            for w in self.cfg["wallets"]
        ]
        text = "<b>Targets</b>\n\n" + pre_table(["Asset", "Target"], rows)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_settarget(self, update, context):
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /settarget &lt;asset or label&gt; &lt;amount&gt;\n"
                "Example: /settarget BTC 2.0",
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
            await update.message.reply_text(f"No wallet matches '{_html.escape(key)}'. See /targets.", parse_mode=ParseMode.HTML)
            return
        if len(matches) > 1:
            labels = ", ".join(_html.escape(display_label(w["label"])) for w in matches)
            await update.message.reply_text(
                f"'{_html.escape(key)}' matches several wallets ({labels}). Use the exact label.",
                parse_mode=ParseMode.HTML,
            )
            return
        wallet = matches[0]
        if amount <= 0:
            await update.message.reply_text("Target must be greater than zero.", parse_mode=ParseMode.HTML)
            return
        if amount < wallet["threshold"]:
            await update.message.reply_text(
                f"Target {fmt_amount(amount)} {wallet['asset']} is below the threshold "
                f"({fmt_amount(wallet['threshold'])} {wallet['asset']}). The target is the top-up "
                "goal and should sit at or above the threshold. Not changed.",
                parse_mode=ParseMode.HTML,
            )
            return
        save_target(self.cfg, wallet["label"], amount)
        asset = wallet["asset"]
        label = _html.escape(display_label(wallet["label"]))
        await update.message.reply_text(
            f"Target updated: {label}\n"
            f"New target: {fmt_amount(amount)} {asset}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_help(self, update, context):
        check = self.cfg["intervals"]["check_minutes"]
        alert_check = self.cfg["intervals"]["alert_check_minutes"]
        await update.message.reply_text(
            "<b>Wallet balance bot</b>\n\n"
            "/balances - scan all wallets now\n"
            "/maxtopup - amounts to bring every wallet up to target (plan only)\n"
            "/thresholds - show thresholds (with targets)\n"
            "/targets - show targets\n"
            "/setthreshold &lt;asset or label&gt; &lt;amount&gt; - change a threshold\n"
            "/settarget &lt;asset or label&gt; &lt;amount&gt; - change a target\n"
            "/help - this message\n\n"
            f"Scans every {check} min. Pings with @tags every scan while any balance is below "
            "threshold, otherwise a brief all-clear when everything is above threshold. "
            f"Also runs a fast check every {alert_check} min in between that stays silent "
            "and only pings if a balance is below threshold. "
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
    app.add_handler(CommandHandler("maxtopup", bot.cmd_maxtopup, filters=only_andrew))
    app.add_handler(CommandHandler("thresholds", bot.cmd_thresholds, filters=only_andrew))
    app.add_handler(CommandHandler("targets", bot.cmd_targets, filters=only_andrew))
    app.add_handler(CommandHandler("setthreshold", bot.cmd_setthreshold, filters=only_andrew))
    app.add_handler(CommandHandler("settarget", bot.cmd_settarget, filters=only_andrew))
    app.add_handler(CommandHandler("help", bot.cmd_help, filters=only_andrew))
    app.add_handler(CommandHandler("start", bot.cmd_help, filters=only_andrew))

    # Scan on a fixed interval, aligned to the next check-interval boundary.
    interval_s = cfg["intervals"]["check_minutes"] * 60
    now = datetime.now(timezone.utc)
    seconds_into_hour = now.minute * 60 + now.second + now.microsecond / 1_000_000
    first_delay = interval_s - (seconds_into_hour % interval_s)
    app.job_queue.run_repeating(bot.tick, interval=interval_s, first=first_delay, name="scan")

    # Fast supplementary check between hourly scans: pings only when low.
    alert_interval_s = cfg["intervals"]["alert_check_minutes"] * 60
    alert_first = alert_interval_s - (seconds_into_hour % alert_interval_s)
    app.job_queue.run_repeating(
        bot.tick_alert, interval=alert_interval_s, first=alert_first, name="alert"
    )

    logger.info(
        "Starting bot: %d wallets, scan every %d min, fast low-only check every %d min, chat %s",
        len(cfg["wallets"]),
        cfg["intervals"]["check_minutes"],
        cfg["intervals"]["alert_check_minutes"],
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
