"""Config loading, validation, and threshold persistence."""

import os
from decimal import Decimal, InvalidOperation

import yaml

from .fetchers import EVM_ASSETS, SUPPORTED_ASSETS


class ConfigError(Exception):
    pass


DEFAULTS = {
    "intervals": {"check_minutes": 30, "alert_check_minutes": 16},
    "heartbeat": {"enabled": True, "time": "09:00", "timezone": "Asia/Singapore"},
    "show_amounts": True,
    "pending_alert": {
        "enabled": True,
        "check_minutes": 5,
        "stuck_minutes": 10,
        "realert_minutes": 30,
        "addresses": [],
    },
}


def load_config(path):
    if not os.path.exists(path):
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml and fill it in."
        )
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = {
        "path": path,
        "intervals": {**DEFAULTS["intervals"], **(raw.get("intervals") or {})},
        "heartbeat": {**DEFAULTS["heartbeat"], **(raw.get("heartbeat") or {})},
        "show_amounts": raw.get("show_amounts", DEFAULTS["show_amounts"]),
        "alert_tags": raw.get("alert_tags") or [],
    }

    telegram = raw.get("telegram") or {}
    cfg["telegram_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or telegram.get("token")
    raw_id = telegram.get("allowed_chat_id")
    if isinstance(raw_id, list):
        cfg["allowed_chat_ids"] = [int(x) for x in raw_id]
    else:
        cfg["allowed_chat_ids"] = [int(raw_id)] if raw_id is not None else []
    # First entry is the primary chat for proactive alerts and heartbeat.
    cfg["allowed_chat_id"] = cfg["allowed_chat_ids"][0] if cfg["allowed_chat_ids"] else None

    wallets = raw.get("wallets") or []
    if not wallets:
        raise ConfigError("No wallets configured.")
    seen_labels = set()
    parsed = []
    for w in wallets:
        label = w.get("label")
        asset = (w.get("asset") or "").upper()
        address = w.get("address")
        threshold = w.get("threshold")
        if not label or not asset or not address or threshold is None:
            raise ConfigError(f"Wallet entry needs label, asset, address, threshold: {w}")
        if asset not in SUPPORTED_ASSETS:
            raise ConfigError(f"Unsupported asset '{asset}' (supported: {', '.join(SUPPORTED_ASSETS)})")
        if label in seen_labels:
            raise ConfigError(f"Duplicate wallet label '{label}'. Labels must be unique.")
        seen_labels.add(label)
        try:
            threshold = Decimal(str(threshold))
        except InvalidOperation:
            raise ConfigError(f"Threshold for '{label}' is not a number: {threshold}")
        target = w.get("target")
        if target is not None:
            try:
                target = Decimal(str(target))
            except InvalidOperation:
                raise ConfigError(f"Target for '{label}' is not a number: {target}")
        parsed.append({
            "label": label,
            "asset": asset,
            "address": address,
            "threshold": threshold,
            "target": target,
        })
    cfg["wallets"] = parsed
    cfg["pending_alert"] = _parse_pending_alert(raw.get("pending_alert"), parsed)
    return cfg


def _parse_pending_alert(raw_pa, wallets):
    """Build the stuck/pending-transaction alert config.

    Defaults apply when the block is absent. The addresses to watch are either an
    explicit `addresses` list, or (when that is empty) auto-derived from the
    configured wallets that live on Ethereum, so the watch always tracks the same
    EVM hot wallet(s) the bot already monitors. Each entry carries a display label
    and the assets sharing that address, so the alert can name them.
    """
    raw_pa = raw_pa or {}
    pa = {**DEFAULTS["pending_alert"], **raw_pa}

    def _pos_int(key, minimum):
        try:
            value = int(pa[key])
        except (TypeError, ValueError):
            raise ConfigError(f"pending_alert.{key} must be a whole number of minutes: {pa[key]!r}")
        if value < minimum:
            raise ConfigError(f"pending_alert.{key} must be at least {minimum}.")
        return value

    enabled = bool(pa.get("enabled", True))
    check_minutes = _pos_int("check_minutes", 1)
    stuck_minutes = _pos_int("stuck_minutes", 0)
    realert_minutes = _pos_int("realert_minutes", 1)

    # Group the EVM wallets by address: assets sharing it, and a display label.
    by_address = {}
    for w in wallets:
        if w["asset"] not in EVM_ASSETS:
            continue
        key = w["address"].lower()
        info = by_address.setdefault(
            key, {"address": w["address"], "assets": [], "labels": [], "eth_label": None}
        )
        if w["asset"] not in info["assets"]:
            info["assets"].append(w["asset"])
        info["labels"].append(w["label"])
        if w["asset"] == "ETH" and info["eth_label"] is None:
            info["eth_label"] = w["label"]

    explicit = pa.get("addresses") or []
    if explicit:
        targets = []
        for addr in explicit:
            info = by_address.get(str(addr).lower())
            targets.append(
                info or {"address": str(addr), "assets": [], "labels": [], "eth_label": None}
            )
    else:
        targets = list(by_address.values())

    addresses = []
    for info in targets:
        label = info["eth_label"] or (info["labels"][0] if info["labels"] else info["address"])
        addresses.append(
            {"address": info["address"], "label": label, "assets": sorted(info["assets"])}
        )

    return {
        "enabled": enabled,
        "check_minutes": check_minutes,
        "stuck_minutes": stuck_minutes,
        "realert_minutes": realert_minutes,
        "addresses": addresses,
    }


def _save_wallet_value(cfg, label, key, value):
    """Persist a single wallet field back to config.yaml without touching other keys."""
    path = cfg["path"]
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    for w in raw.get("wallets") or []:
        if w.get("label") == label:
            w[key] = float(value)
            break
    else:
        raise ConfigError(f"Wallet '{label}' not found in {path}")
    with open(path, "w") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, default_flow_style=False)
    for w in cfg["wallets"]:
        if w["label"] == label:
            w[key] = value


def save_threshold(cfg, label, new_threshold):
    """Persist a threshold change back to config.yaml."""
    _save_wallet_value(cfg, label, "threshold", new_threshold)


def save_target(cfg, label, new_target):
    """Persist a target change back to config.yaml."""
    _save_wallet_value(cfg, label, "target", new_target)


def save_interval(cfg, key, minutes):
    """Persist an interval (whole minutes) under the `intervals` block of config.yaml."""
    path = cfg["path"]
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    intervals = raw.get("intervals") or {}
    intervals[key] = int(minutes)
    raw["intervals"] = intervals
    with open(path, "w") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, default_flow_style=False)
    cfg["intervals"][key] = int(minutes)


def find_wallets(cfg, key):
    """Match wallets by exact label first, then by asset symbol."""
    by_label = [w for w in cfg["wallets"] if w["label"].lower() == key.lower()]
    if by_label:
        return by_label
    return [w for w in cfg["wallets"] if w["asset"] == key.upper()]
