"""Unit tests for chains: config parsing, the Chain column, asset+chain lookup and
the Tron balance readers.

Network-free: Tron replies are faked by replacing fetchers._post_json. Run from
the app directory:

    python tests/test_chains.py        # standalone, prints a summary
    python -m pytest tests/test_chains.py   # if pytest is installed

Importing bot.main pulls in python-telegram-bot (a runtime dependency), so run
this inside the bot's virtualenv.

The only addresses here are a made-up Ethereum address and the public USDT
token contract on Tron, used because it is a valid Tron address. No hot wallet
address is in this file. sensitive-data-guard:allow
"""

import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import fetchers  # noqa: E402
from bot.config import ConfigError, find_wallets, load_config  # noqa: E402
from bot.main import build_low_alert, build_scan, build_topup_plan, display_name  # noqa: E402

EVM_ADDR = "0x" + "11" * 20
TRON_ADDR = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_TYPO = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u"  # last character changed

# The wallets block of the live config before chains existed: no `chain:` keys.
LEGACY_WALLETS = f"""
wallets:
  - label: BTC hot wallet
    asset: BTC
    address: bc1qexample
    threshold: 0.8
    target: 1.7
  - label: ETH hot wallet
    asset: ETH
    address: "{EVM_ADDR}"
    threshold: 24
    target: 48
  - label: SOL hot wallet
    asset: SOL
    address: solexample
    threshold: 150
    target: 300
  - label: XRP hot wallet
    asset: XRP
    address: rexample
    threshold: 9500
    target: 19000
  - label: LTC hot wallet
    asset: LTC
    address: ltc1qexample
    threshold: 235
    target: 470
  - label: USDT hot wallet
    asset: USDT
    address: "{EVM_ADDR}"
    threshold: 30000
    target: 50000
  - label: USDC hot wallet
    asset: USDC
    address: "{EVM_ADDR}"
    threshold: 30000
    target: 50000
"""

TRON_WALLETS = f"""
  - label: TRX hot wallet
    asset: TRX
    chain: TRX
    address: {TRON_ADDR}
    threshold: 50
    target: 300
  - label: USDT TRX hot wallet
    asset: USDT
    chain: tron
    address: {TRON_ADDR}
    threshold: 2000
    target: 10000
"""


def _load(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.yaml")
        with open(path, "w") as fh:
            fh.write("telegram:\n  allowed_chat_id: 1\n" + text)
        return load_config(path)


def _raises_config_error(text, fragment):
    try:
        _load(text)
    except ConfigError as exc:
        assert fragment in str(exc), str(exc)
        return
    raise AssertionError("expected ConfigError")


def _wallet(asset, chain, threshold, target=None, label=None):
    return {
        "label": label or f"{asset} {chain} hot wallet",
        "asset": asset,
        "chain": chain,
        "address": "x",
        "threshold": Decimal(threshold),
        "target": Decimal(target) if target is not None else None,
    }


# -- config ----------------------------------------------------------------


def test_legacy_config_parses_with_default_chains():
    cfg = _load(LEGACY_WALLETS)
    chains = {w["label"]: w["chain"] for w in cfg["wallets"]}
    assert chains == {
        "BTC hot wallet": "BTC",
        "ETH hot wallet": "ETH",
        "SOL hot wallet": "SOL",
        "XRP hot wallet": "XRP",
        "LTC hot wallet": "LTC",
        "USDT hot wallet": "ETH",
        "USDC hot wallet": "ETH",
    }


def test_tron_wallets_parse_with_code_and_alias():
    cfg = _load(LEGACY_WALLETS + TRON_WALLETS)
    tron = [(w["asset"], w["chain"]) for w in cfg["wallets"] if w["address"] == TRON_ADDR]
    assert tron == [("TRX", "TRX"), ("USDT", "TRX")]


def test_unsupported_pair_rejected():
    text = f"""
wallets:
  - label: USDC TRX hot wallet
    asset: USDC
    chain: TRX
    address: {TRON_ADDR}
    threshold: 1
"""
    _raises_config_error(text, "Unsupported asset and chain 'USDC on TRX'")


def test_unknown_chain_rejected():
    text = f"""
wallets:
  - label: USDT BSC hot wallet
    asset: USDT
    chain: BSC
    address: "{EVM_ADDR}"
    threshold: 1
"""
    _raises_config_error(text, "Unknown chain 'BSC'")


def test_tron_address_typo_rejected():
    text = f"""
wallets:
  - label: TRX hot wallet
    asset: TRX
    address: {TRON_TYPO}
    threshold: 1
"""
    _raises_config_error(text, "checksum")


def test_pending_watch_only_takes_ethereum_wallets():
    cfg = _load(LEGACY_WALLETS + TRON_WALLETS)
    addresses = cfg["pending_alert"]["addresses"]
    assert len(addresses) == 1
    assert addresses[0]["address"] == EVM_ADDR
    assert addresses[0]["assets"] == ["ETH", "USDC", "USDT"]


# -- lookup by asset and chain -----------------------------------------------


def test_find_wallets_asset_and_chain():
    cfg = _load(LEGACY_WALLETS + TRON_WALLETS)
    assert len(find_wallets(cfg, "USDT")) == 2
    for key in ("USDT TRX", "usdt-tron", "USDT/TRX", "USDT (TRX)"):
        found = find_wallets(cfg, key)
        assert [(w["asset"], w["chain"]) for w in found] == [("USDT", "TRX")], key
    found = find_wallets(cfg, "USDT ETH")
    assert [w["label"] for w in found] == ["USDT hot wallet"]
    assert [w["label"] for w in find_wallets(cfg, "USDT TRX hot wallet")] == ["USDT TRX hot wallet"]
    assert [w["label"] for w in find_wallets(cfg, "TRX")] == ["TRX hot wallet"]
    assert [w["label"] for w in find_wallets(cfg, "btc")] == ["BTC hot wallet"]


# -- messages ----------------------------------------------------------------


def test_display_name():
    assert display_name(_wallet("BTC", "BTC", 1)) == "BTC"
    assert display_name(_wallet("TRX", "TRX", 1)) == "TRX"
    assert display_name(_wallet("USDT", "ETH", 1)) == "USDT (ETH)"
    assert display_name(_wallet("USDT", "TRX", 1)) == "USDT (TRX)"


def test_build_scan_has_chain_column_and_two_usdt_rows():
    results = [
        (_wallet("USDT", "ETH", 30000, 100000), Decimal("82182.04"), None),
        (_wallet("USDT", "TRX", 8000, 10000), Decimal("7150.57"), None),
        (_wallet("TRX", "TRX", 50, 300), None, fetchers.FetchError("down")),
    ]
    text = build_scan(results)
    table = text.split("<pre>")[1].split("</pre>")[0].split("\n")
    assert table[0].split() == ["Asset", "Chain", "Balance", "Full"], table[0]
    assert table[1].split()[:4] == ["USDT", "ETH", "82,182.04", "82.2%"] and table[1].endswith("✅")
    assert table[2].split()[:4] == ["USDT", "TRX", "7,150.57", "71.5%"] and table[2].endswith("❌")
    assert table[3].split()[:2] == ["TRX", "TRX"] and "fetch failed" in table[3] and table[3].endswith("⚠️")
    assert "OK" not in table[1] and "LOW" not in table[2]
    assert "Top ups needed:" in text
    assert "USDT (TRX): 2,849.43 USDT  (target 10,000.00 USDT)" in text


def test_build_scan_without_amounts():
    text = build_scan([(_wallet("USDT", "TRX", 1), Decimal("5"), None)], show_amounts=False)
    table = text.split("<pre>")[1].split("</pre>")[0].split("\n")
    assert table[0].split() == ["Asset", "Chain"]
    assert table[1].split()[:2] == ["USDT", "TRX"] and "5.00" not in text


def test_low_alert_and_topup_plan_name_the_chain():
    wallet = _wallet("USDT", "TRX", 8000, 10000)
    alert = build_low_alert([(wallet, Decimal("7150.57"))])
    assert "USDT (TRX)" in alert
    plan = build_topup_plan([(wallet, Decimal("7150.57"), None)])
    header = plan.split("<pre>")[1].split("\n")[0].split()
    assert header == ["Asset", "Chain", "Top", "up", "Current", "Target"], header
    assert "TRX" in plan and "+2,849.43" in plan


# -- Tron readers --------------------------------------------------------------


class _FakePost:
    """Stands in for fetchers._post_json: returns a reply per URL suffix, or raises."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        for suffix, reply in self.replies.items():
            if url.endswith(suffix):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise AssertionError(f"unexpected call to {url}")


def _with_fake(replies, fn):
    original = fetchers._post_json
    fake = _FakePost(replies)
    fetchers._post_json = fake
    try:
        return fn(), fake
    finally:
        fetchers._post_json = original


def test_tron_address_bytes():
    raw = fetchers.tron_address_bytes(TRON_ADDR)
    assert len(raw) == 21 and raw[0] == 0x41
    for bad in (TRON_TYPO, "T0O", EVM_ADDR):
        try:
            fetchers.tron_address_bytes(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_trx_balance_read():
    value, _ = _with_fake(
        {"/wallet/getaccount": {"address": TRON_ADDR, "balance": 66443636}},
        lambda: fetchers.get_balance("TRX", TRON_ADDR, "TRX"),
    )
    assert value == Decimal("66.443636")


def test_trx_activated_account_without_balance_reads_zero():
    value, _ = _with_fake(
        {"/wallet/getaccount": {"address": TRON_ADDR, "create_time": 1}},
        lambda: fetchers.get_balance("TRX", TRON_ADDR, "TRX"),
    )
    assert value == Decimal(0)


def test_trx_unknown_account_is_a_failed_read():
    try:
        _with_fake({"/wallet/getaccount": {}}, lambda: fetchers.get_balance("TRX", TRON_ADDR, "TRX"))
    except fetchers.FetchError as exc:
        assert "not found" in str(exc)
        return
    raise AssertionError("expected FetchError")


def test_trc20_usdt_balance_read():
    reply = {"result": {"result": True}, "constant_result": [format(7150579300, "064x")]}
    value, fake = _with_fake(
        {"/wallet/triggerconstantcontract": reply},
        lambda: fetchers.get_balance("USDT", TRON_ADDR, "TRX"),
    )
    assert value == Decimal("7150.5793")
    payload = fake.calls[0][1]
    assert payload["function_selector"] == "balanceOf(address)"
    assert payload["contract_address"] == TRON_ADDR
    assert len(payload["parameter"]) == 64


def test_trc20_failed_call_is_a_failed_read():
    reply = {"result": {"result": False}}
    try:
        _with_fake({"/wallet/triggerconstantcontract": reply}, lambda: fetchers.get_balance("USDT", TRON_ADDR, "TRX"))
    except fetchers.FetchError:
        return
    raise AssertionError("expected FetchError")


def test_tron_falls_back_to_second_endpoint():
    primary = fetchers.TRON_ENDPOINTS[0] + "/wallet/getaccount"
    fallback = fetchers.TRON_ENDPOINTS[1] + "/wallet/getaccount"
    original = fetchers._post_json
    calls = []

    def fake(url, payload):
        calls.append(url)
        if url == primary:
            raise OSError("timeout")
        return {"address": TRON_ADDR, "balance": 1_000_000}

    fetchers._post_json = fake
    try:
        value = fetchers.get_balance("TRX", TRON_ADDR, "TRX")
    finally:
        fetchers._post_json = original
    assert value == Decimal(1)
    assert calls == [primary, fallback]


def test_usdt_still_defaults_to_ethereum():
    assert fetchers.DEFAULT_CHAIN["USDT"] == "ETH"
    assert fetchers.FETCHERS[("USDT", "ETH")][0].__name__ == "_usdt_publicnode"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
