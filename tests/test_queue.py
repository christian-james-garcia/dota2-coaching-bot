"""Queue policy: back off on transient failures, park on defects.

The backoff ceiling used to be expressed twice, with the enclosing cap
unreachable. And every handler caught bare Exception, so a logic error was
indistinguishable from a service outage and was retried on a timer forever.
"""
import pathlib
import tempfile
import time

from _support import main_module


def _isolated_state():
    bot = main_module()
    bot.STATE_FILE = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    bot.MATCH_STATE = {
        "accounts": {},
        "pending": [
            {"account_id": 1, "match_id": 100},
            {"account_id": 1, "match_id": 101},
        ],
    }
    return bot


def _item(bot, match_id):
    return next(i for i in bot.MATCH_STATE["pending"] if i["match_id"] == match_id)


def _eligible(bot):
    now = time.time()
    return [
        i["match_id"] for i in bot.MATCH_STATE["pending"]
        if float(i.get("next_attempt_at", 0)) <= now and not i.get("blocked")
    ]


def test_backoff_doubles_then_holds_at_the_ceiling():
    bot = _isolated_state()
    delays = []
    for _ in range(8):
        before = time.time()
        bot.defer_pending_match(1, 100, "a transient service failure")
        delays.append(round(_item(bot, 100)["next_attempt_at"] - before))
    assert delays == [60, 120, 240, 480, 960, 1920, 1920, 1920]
    assert max(delays) == bot.RETRY_BACKOFF_MAX_SECONDS


def test_transient_failures_never_park_a_match():
    bot = _isolated_state()
    for _ in range(bot.BUG_RETRY_LIMIT + 5):
        bot.defer_pending_match(1, 100, "a transient service failure")
    assert not _item(bot, 100).get("blocked")


def test_a_defect_parks_the_match_at_the_limit():
    bot = _isolated_state()
    for attempt in range(1, bot.BUG_RETRY_LIMIT):
        bot.defer_pending_match(1, 100, "an unexpected processing error", fatal=True)
        assert not _item(bot, 100).get("blocked"), f"parked early on attempt {attempt}"
    bot.defer_pending_match(1, 100, "an unexpected processing error", fatal=True)
    assert _item(bot, 100)["blocked"] is True


def test_a_parked_match_does_not_hold_up_the_queue():
    bot = _isolated_state()
    for _ in range(bot.BUG_RETRY_LIMIT):
        bot.defer_pending_match(1, 100, "an unexpected processing error", fatal=True)
    _item(bot, 100)["next_attempt_at"] = 0
    eligible = _eligible(bot)
    assert 100 not in eligible
    assert 101 in eligible


def test_state_round_trips_and_a_restart_releases_parked_matches():
    bot = _isolated_state()
    for _ in range(bot.BUG_RETRY_LIMIT):
        bot.defer_pending_match(1, 100, "an unexpected processing error", fatal=True)
    bot.save_match_state()
    assert not list(bot.STATE_FILE.parent.glob("*.tmp")), "atomic write left a temp file"

    reloaded = bot.load_match_state()
    assert any(i.get("blocked") for i in reloaded["pending"])

    released = bot.clear_blocked_flags(reloaded)
    assert not any(i.get("blocked") for i in released["pending"])
    assert all(i.get("attempts", 0) == 0 for i in released["pending"])


def test_a_corrupt_state_file_yields_empty_state():
    bot = _isolated_state()
    bot.STATE_FILE.write_text("{not json", encoding="utf-8")
    assert bot.load_match_state() == {"accounts": {}, "pending": []}


def test_a_missing_state_file_yields_empty_state():
    bot = _isolated_state()
    assert bot.load_match_state() == {"accounts": {}, "pending": []}


def test_logic_errors_are_not_classified_as_transient():
    # This is the guard that keeps the two absorbed bugs from returning.
    bot = main_module()
    for kind in (KeyError, TypeError, AttributeError, IndexError, ValueError):
        assert not issubclass(kind, bot.TRANSIENT_ERRORS), kind.__name__


def test_service_failures_are_classified_as_transient():
    bot = main_module()
    for kind in (bot.aiohttp.ClientError, TimeoutError, OSError):
        assert issubclass(kind, bot.TRANSIENT_ERRORS), kind.__name__
