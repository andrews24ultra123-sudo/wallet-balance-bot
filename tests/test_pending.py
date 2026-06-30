"""Unit tests for the stuck/pending-withdrawal state machine and alert text.

Network-free: it only exercises the pure `evaluate_pending` decision function and
the message builders. Run from the app directory:

    python tests/test_pending.py        # standalone, prints a summary
    python -m pytest tests/test_pending.py   # if pytest is installed

Importing bot.main pulls in python-telegram-bot (a runtime dependency), so run
this inside the bot's virtualenv.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.main import (  # noqa: E402
    _assets_phrase,
    build_pending_alert,
    build_pending_cleared,
    evaluate_pending,
)

S = 60  # seconds per minute
STUCK = 10
REALERT = 30


def _state(first_seen, base, alerted, last_alert):
    return {
        "first_seen_ts": first_seen,
        "latest_nonce_at_first_seen": base,
        "alerted": alerted,
        "last_alert_ts": last_alert,
    }


# -- evaluate_pending: the six worked-example checkpoints, plus edges ---------


def test_no_pending_no_prev():
    action, state = evaluate_pending(None, 1000, 1000, 0.0, STUCK, REALERT)
    assert action is None and state is None


def test_new_backlog_starts_timer():
    action, state = evaluate_pending(None, 1000, 1003, 0.0, STUCK, REALERT)
    assert action is None
    assert state == _state(0.0, 1000, False, None)


def test_below_threshold_stays_silent():
    prev = _state(0.0, 1000, False, None)
    action, state = evaluate_pending(prev, 1000, 1005, 5 * S, STUCK, REALERT)
    assert action is None
    assert state["alerted"] is False
    assert state["first_seen_ts"] == 0.0  # timer not restarted


def test_alert_fires_at_threshold():
    prev = _state(0.0, 1000, False, None)
    action, state = evaluate_pending(prev, 1000, 1006, 10 * S, STUCK, REALERT)
    assert action == "alert"
    assert state["alerted"] is True
    assert state["last_alert_ts"] == 10 * S


def test_silent_within_reminder_window():
    prev = _state(0.0, 1000, True, 10 * S)
    action, state = evaluate_pending(prev, 1000, 1006, 10 * S + 15 * S, STUCK, REALERT)
    assert action is None
    assert state["last_alert_ts"] == 10 * S  # unchanged


def test_reminder_fires_after_realert_window():
    prev = _state(0.0, 1000, True, 10 * S)
    action, state = evaluate_pending(prev, 1000, 1006, 10 * S + 30 * S, STUCK, REALERT)
    assert action == "realert"
    assert state["last_alert_ts"] == 10 * S + 30 * S


def test_sudden_drain_clears():
    prev = _state(0.0, 1000, True, 10 * S)
    action, state = evaluate_pending(prev, 1007, 1007, 60 * S, STUCK, REALERT)
    assert action == "clear" and state is None


def test_gradual_drain_after_alert_clears():
    # Mined nonce advances but a backlog is still draining: still counts as cleared.
    prev = _state(0.0, 1000, True, 10 * S)
    action, state = evaluate_pending(prev, 1003, 1006, 50 * S, STUCK, REALERT)
    assert action == "clear" and state is None


def test_moving_before_alert_resets_timer_without_clearing():
    # Queue advancing before we ever alerted: restart the clock, no message.
    prev = _state(0.0, 1000, False, None)
    action, state = evaluate_pending(prev, 1001, 1004, 7 * S, STUCK, REALERT)
    assert action is None
    assert state == _state(7 * S, 1001, False, None)


def test_brief_pending_clears_without_alert():
    prev = _state(0.0, 1000, False, None)
    action, state = evaluate_pending(prev, 1000, 1000, 2 * S, STUCK, REALERT)
    assert action is None and state is None


# -- message builders --------------------------------------------------------


def test_assets_phrase():
    assert _assets_phrase([]) == ""
    assert _assets_phrase(["ETH"]) == "ETH"
    assert _assets_phrase(["ETH", "USDC"]) == "ETH and USDC"
    assert _assets_phrase(["ETH", "USDC", "USDT"]) == "ETH, USDC and USDT"


def test_build_pending_alert_content():
    entry = {"label": "ETH hot wallet", "assets": ["ETH", "USDC", "USDT"]}
    text = build_pending_alert(entry, 6, 10, tags=["@mrpotato1234", "@roystontham"])
    assert "WITHDRAWALS BLOCKED" in text
    assert "ETH hot wallet" in text  # full label as the subject line
    assert "At least 6 transactions pending" in text
    assert "about 10 minutes" in text
    assert "ETH, USDC and USDT withdrawals from this wallet are all blocked." in text
    assert "drop-and-replace" in text
    assert "@roystontham" in text
    # Style guardrails: no emoji dash artefacts, no em dash.
    assert "—" not in text


def test_build_pending_alert_reminder_and_singular():
    entry = {"label": "ETH hot wallet", "assets": ["ETH"]}
    text = build_pending_alert(entry, 1, 40, tags=None, reminder=True)
    assert "STILL BLOCKED" in text
    assert "At least 1 transaction pending" in text  # singular
    assert "ETH withdrawals from this wallet is blocked." in text


def test_build_pending_cleared_content():
    entry = {"label": "ETH hot wallet", "assets": ["ETH", "USDC", "USDT"]}
    text = build_pending_cleared(entry)
    assert "Withdrawal queue cleared." in text
    assert "ETH hot wallet" in text
    assert "advancing again" in text


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
