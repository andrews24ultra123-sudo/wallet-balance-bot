"""Config loading, validation, and threshold persistence."""

import os
from decimal import Decimal, InvalidOperation

import yaml

from .fetchers import SUPPORTED_ASSETS


class ConfigError(Exception):
    pass


DEFAULTS = {
    "intervals": {"check_minutes": 30, "alert_check_minutes": 16},
    "heartbeat": {"enabled": True, "time": "09:00", "timezone": "Asia/Singapore"},
    "show_amounts": True,
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
    return cfg


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
