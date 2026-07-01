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
from .config import ConfigError, find_wallets, load_config, save_interval, save_target, save_threshold

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
# Stuck / pending withdrawal detection


def _assets_phrase(assets):
    """'ETH', 'ETH and USDC', or 'ETH, USDC and USDT'."""
    if not assets:
        return ""
    if len(assets) == 1:
        return assets[0]
    return ", ".join(assets[:-1]) + " and " + assets[-1]


def build_pending_alert(entry, gap, minutes_stuck, tags=None, reminder=False):
    """Alert that a wallet's withdrawal queue is stuck (transactions not clearing).

    Alert-only: it tells the operator to act in Fireblocks. The bot never moves,
    signs, or replaces anything itself.
    """
    label = _html.escape(entry["label"])  # full label; this is the alert's subject line
    assets = entry.get("assets") or []
    phrase = _assets_phrase(assets)
    noun = "transaction" if gap == 1 else "transactions"
    header = "STILL BLOCKED: stuck ETH transactions" if reminder else "WITHDRAWALS BLOCKED: stuck ETH transactions"
    lines = [
        f"<b>{header}</b>",
        "",
        label,
        f"At least {gap} {noun} pending and not clearing.",
        f"Stuck for about {minutes_stuck} minutes (mined nonce has not advanced).",
    ]
    if phrase:
        verb = "is" if len(assets) == 1 else "are all"
        lines.append(f"{phrase} withdrawals from this wallet {verb} blocked.")
    lines += [
        "",
        "Action: in Fireblocks, drop-and-replace the oldest stuck transaction "
        "(the lowest nonce) from this wallet to clear the queue.",
    ]
    tag_line = _tags_line(tags)
    if tag_line:
        lines += ["", tag_line]
    return "\n".join(lines)


def build_pending_cleared(entry):
    """Confirmation that a previously-stuck queue has drained."""
    label = _html.escape(entry["label"])
    phrase = _assets_phrase(entry.get("assets") or [])
    can = f" {phrase} withdrawals can go out." if phrase else ""
    return (
        "<b>Withdrawal queue cleared.</b>\n\n"
        f"{label}: no pending transactions, mined nonce advancing again.{can}"
    )


def _pending_status_line(entry, info):
    """One line describing a wallet's current queue status. info is a gap dict or None."""
    label = _html.escape(entry["label"])
    if info is None:
        return f"{label}: queue status unavailable"
    if info["gap"] > 0:
        noun = "transaction" if info["gap"] == 1 else "transactions"
        return f"{label}: {info['gap']} {noun} pending (mined nonce {info['latest']})"
    return f"{label}: clear (mined nonce {info['latest']})"


def build_pending_summary(statuses):
    """Compact queue-status block for the daily heartbeat. statuses: [(entry, info_or_None)]."""
    lines = ["<b>Withdrawal queue</b>"]
    for entry, info in statuses:
        lines.append(_pending_status_line(entry, info))
    return "\n".join(lines)


def build_pending_allclear(statuses):
    """Standalone periodic confirmation that no withdrawals are stuck."""
    lines = ["<b>No stuck withdrawals.</b>", ""]
    for entry, info in statuses:
        lines.append(_pending_status_line(entry, info))
    return "\n".join(lines)


def evaluate_pending(prev, latest, pending, now, stuck_minutes, realert_minutes):
    """Decide what to do about an address's pending queue. Pure: no I/O.

    prev: previous state dict for this address, or None.
    latest / pending: mined and mempool-inclusive nonce.
    now: epoch seconds.

    Returns (action, new_state) where action is one of
    None | "alert" | "realert" | "clear", and new_state is the dict to store for
    this address, or None to clear the stored entry.
    """
    gap = pending - latest
    if gap <= 0:
        # Queue empty or drained.
        if prev and prev.get("alerted"):
            return "clear", None
        return None, None

    started = prev.get("first_seen_ts") if prev else None
    base_nonce = prev.get("latest_nonce_at_first_seen") if prev else None
    moving = started is None or base_nonce is None or latest > base_nonce
    if moving:
        if prev and prev.get("alerted"):
            # We alerted on a stall and the mined nonce is advancing again: the
            # blockage has cleared (even if some backlog is still draining).
            # Confirm recovery and reset; a fresh stall starts a new clock.
            return "clear", None
        # New backlog, or the mined nonce advanced since we started timing: the
        # front of the queue is being mined, so (re)start the clock; do not alert.
        return None, {
            "first_seen_ts": now,
            "latest_nonce_at_first_seen": latest,
            "alerted": False,
            "last_alert_ts": None,
        }

    # Backlog present and the mined nonce has not advanced since first_seen.
    new_state = dict(prev)
    elapsed = now - started
    if not prev.get("alerted"):
        if elapsed >= stuck_minutes * 60:
            new_state["alerted"] = True
            new_state["last_alert_ts"] = now
            return "alert", new_state
        return None, new_state
    last = prev.get("last_alert_ts") or started
    if now - last >= realert_minutes * 60:
        new_state["last_alert_ts"] = now
        return "realert", new_state
    return None, new_state


def evaluate_allclear(last_ts, now, allclear_hours, all_clear, activity):
    """Decide whether to send the periodic 'no stuck withdrawals' confirmation. Pure.

    Returns (should_send, new_last_ts). Sends only when the queue is fully clear,
    nothing fired this cycle, and at least allclear_hours have passed since the last
    confirmation. On activity (a stuck alert/reminder/cleared) or the first healthy
    observation it rebases the timer without sending, to avoid spam.
    """
    if not allclear_hours:
        return False, last_ts
    if activity:
        return False, now
    if not all_clear:
        return False, last_ts
    if last_ts is None:
        return False, now
    if now - last_ts >= allclear_hours * 3600:
        return True, now
    return False, last_ts


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

    async def fetch_gap(self, address):
        return await asyncio.to_thread(fetchers.get_eth_pending_gap, address)

    async def pending_statuses(self):
        """Live queue status for each monitored address: [(entry, gap_info_or_None)]."""
        out = []
        for entry in self.cfg["pending_alert"]["addresses"]:
            try:
                info = await self.fetch_gap(entry["address"])
            except fetchers.FetchError:
                info = None
            out.append((entry, info))
        return out

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

    async def tick_pending(self, context):
        """Watch each monitored EVM address for a stuck withdrawal queue.

        Reads the mined-vs-pending nonce gap and alerts when the queue stops
        draining. This job persists its own keys under state['_pending_tx'] and
        calls save_state. It is safe alongside `tick`: the two run one at a time
        on the same event loop and mutate disjoint state keys, so neither loses
        the other's writes (do not reassign self.state wholesale in either job).
        Strictly read-only on-chain: it never signs, sends, or replaces anything.
        """
        pa = self.cfg["pending_alert"]
        bot = context.bot
        tags = self.cfg.get("alert_tags") or []
        stuck_minutes = pa["stuck_minutes"]
        realert_minutes = pa["realert_minutes"]
        pending_state = self.state.setdefault("_pending_tx", {})
        now = datetime.now(timezone.utc).timestamp()
        statuses = []      # (entry, gap_info) for addresses successfully read this cycle
        activity = False   # a stuck alert, reminder, or cleared message fired this cycle

        for entry in pa["addresses"]:
            address = entry["address"]
            try:
                gap_info = await self.fetch_gap(address)
            except fetchers.FetchError as exc:
                logger.warning("Pending check fetch failed (%s): %s", entry["label"], exc)
                continue
            statuses.append((entry, gap_info))

            action, new_state = evaluate_pending(
                pending_state.get(address),
                gap_info["latest"],
                gap_info["pending"],
                now,
                stuck_minutes,
                realert_minutes,
            )
            if new_state is None:
                pending_state.pop(address, None)
            else:
                pending_state[address] = new_state

            if action in ("alert", "realert"):
                activity = True
                minutes_stuck = max(0, int((now - new_state["first_seen_ts"]) // 60))
                await self.send(
                    bot,
                    build_pending_alert(
                        entry, gap_info["gap"], minutes_stuck, tags,
                        reminder=(action == "realert"),
                    ),
                )
            elif action == "clear":
                activity = True
                await self.send(bot, build_pending_cleared(entry))

        # Periodic positive confirmation that the queue is flowing (only when every
        # monitored address was read and is clear, and nothing fired this cycle).
        all_clear = len(statuses) == len(pa["addresses"]) and all(info["gap"] == 0 for _, info in statuses)
        send_allclear, new_last = evaluate_allclear(
            self.state.get("_pending_allclear_ts"), now, pa.get("allclear_hours") or 0, all_clear, activity
        )
        self.state["_pending_allclear_ts"] = new_last
        if send_allclear:
            await self.send(bot, build_pending_allclear(statuses))

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
        text = await self.scan_text(title=f"Daily heartbeat: {date_str}")
        pa = self.cfg["pending_alert"]
        if pa["enabled"] and pa["addresses"]:
            text += "\n\n" + build_pending_summary(await self.pending_statuses())
        await self.send(bot, text)

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

    async def cmd_setalertcheck(self, update, context):
        args = context.args or []
        if len(args) != 1:
            await update.message.reply_text(
                "Usage: /setalertcheck &lt;minutes&gt;\n"
                "Example: /setalertcheck 15",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            minutes = int(args[0])
        except ValueError:
            await update.message.reply_text(
                f"'{_html.escape(args[0])}' is not a whole number of minutes.",
                parse_mode=ParseMode.HTML,
            )
            return
        scan = self.cfg["intervals"]["check_minutes"]
        if minutes < 1:
            await update.message.reply_text(
                "The fast check interval must be at least 1 minute. Not changed.",
                parse_mode=ParseMode.HTML,
            )
            return
        if minutes > scan:
            await update.message.reply_text(
                f"The fast check ({minutes} min) should not be slower than the main scan "
                f"({scan} min), or it adds nothing. Not changed.",
                parse_mode=ParseMode.HTML,
            )
            return

        save_interval(self.cfg, "alert_check_minutes", minutes)

        # Reschedule the live job so the change takes effect now, without a restart.
        # The hourly "scan" job is left untouched.
        for job in context.job_queue.get_jobs_by_name("alert"):
            job.schedule_removal()
        alert_interval_s = minutes * 60
        now = datetime.now(timezone.utc)
        seconds_into_hour = now.minute * 60 + now.second + now.microsecond / 1_000_000
        alert_first = alert_interval_s - (seconds_into_hour % alert_interval_s)
        context.job_queue.run_repeating(
            self.tick_alert, interval=alert_interval_s, first=alert_first, name="alert"
        )

        note = ""
        if 60 % minutes:
            note = (
                " Note: this does not divide evenly into 60 min, so it will occasionally "
                "land in the same minute as the main scan (a duplicate low alert, only when "
                "something is already low)."
            )
        await update.message.reply_text(
            f"Fast check interval updated to {minutes} min and now live.{note}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_pending(self, update, context):
        """On-demand check of the monitored wallet(s) for stuck/pending withdrawals."""
        pa = self.cfg["pending_alert"]
        if not pa["addresses"]:
            await update.message.reply_text(
                "No Ethereum wallets are configured to check for stuck transactions.",
                parse_mode=ParseMode.HTML,
            )
            return
        await update.message.reply_text("Checking for stuck transactions...", parse_mode=ParseMode.HTML)
        lines = ["<b>Pending transaction check</b>", ""]
        for entry in pa["addresses"]:
            label = _html.escape(entry["label"])
            try:
                gap_info = await self.fetch_gap(entry["address"])
            except fetchers.FetchError:
                lines += [f"{label}: could not read right now.", ""]
                continue
            if gap_info["gap"] > 0:
                noun = "transaction" if gap_info["gap"] == 1 else "transactions"
                lines += [
                    f"{label}: at least {gap_info['gap']} {noun} pending.",
                    f"Mined nonce {gap_info['latest']}, pending nonce {gap_info['pending']}.",
                    "",
                ]
            else:
                lines += [f"{label}: no pending backlog (mined nonce {gap_info['latest']}).", ""]
        await update.message.reply_text("\n".join(lines).rstrip(), parse_mode=ParseMode.HTML)

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
            "/setalertcheck &lt;minutes&gt; - change the fast-check interval\n"
            "/pending - check the hot wallet for stuck/pending withdrawals now\n"
            "/help - this message\n\n"
            f"Scans every {check} min. Pings with @tags every scan while any balance is below "
            "threshold, otherwise a brief all-clear when everything is above threshold. "
            f"Also runs a fast check every {alert_check} min in between that stays silent "
            "and only pings if a balance is below threshold. "
            "Separately watches the ETH hot wallet for stuck/pending withdrawals and alerts "
            "if the queue stops clearing, and confirms it is clear in the daily heartbeat and "
            "periodically. "
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
    app.add_handler(CommandHandler("setalertcheck", bot.cmd_setalertcheck, filters=only_andrew))
    app.add_handler(CommandHandler("pending", bot.cmd_pending, filters=only_andrew))
    app.add_handler(CommandHandler("stuck", bot.cmd_pending, filters=only_andrew))
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

    # Stuck-withdrawal watch: alert if a monitored EVM wallet's pending queue
    # stops draining. Skipped if disabled or no EVM wallets are configured.
    pa = cfg["pending_alert"]
    if pa["enabled"] and pa["addresses"]:
        pending_interval_s = pa["check_minutes"] * 60
        pending_first = pending_interval_s - (seconds_into_hour % pending_interval_s)
        app.job_queue.run_repeating(
            bot.tick_pending, interval=pending_interval_s, first=pending_first, name="pending"
        )
        logger.info(
            "Stuck-withdrawal watch on %d address(es), every %d min (stuck after %d min)",
            len(pa["addresses"]), pa["check_minutes"], pa["stuck_minutes"],
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
