"""Queue policy: back off on transient failures, park on defects.

The backoff ceiling used to be expressed twice, with the enclosing cap
unreachable. And every handler caught bare Exception, so a logic error was
indistinguishable from a service outage and was retried on a timer forever.
"""
import pathlib
import tempfile
import time

from _support import main_module, state_module


def _queue():
    """A MatchState on a throwaway path, holding two queued matches."""
    MatchState = state_module().MatchState
    path = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    return MatchState(path, pending=[
        {"account_id": 1, "match_id": 100},
        {"account_id": 1, "match_id": 101},
    ])


def test_backoff_doubles_then_holds_at_the_ceiling():
    queue = _queue()
    delays = []
    for _ in range(8):
        before = time.time()
        queue.defer(1, 100, "a transient service failure")
        delays.append(round(queue.find(1, 100)["next_attempt_at"] - before))
    assert delays == [60, 120, 240, 480, 960, 1920, 1920, 1920]


def test_backoff_formula_is_capped():
    state = state_module()
    assert [state.backoff_seconds(n) for n in range(1, 9)] == \
        [60, 120, 240, 480, 960, 1920, 1920, 1920]
    assert state.backoff_seconds(40) == state.RETRY_BACKOFF_MAX_SECONDS


def test_transient_failures_never_park_a_match():
    queue = _queue()
    for _ in range(state_module().BUG_RETRY_LIMIT + 5):
        queue.defer(1, 100, "a transient service failure")
    assert not queue.find(1, 100).get("blocked")


def test_a_defect_parks_the_match_at_the_limit():
    queue = _queue()
    limit = state_module().BUG_RETRY_LIMIT
    for attempt in range(1, limit):
        queue.defer(1, 100, "an unexpected processing error", fatal=True)
        assert not queue.find(1, 100).get("blocked"), f"parked early on attempt {attempt}"
    queue.defer(1, 100, "an unexpected processing error", fatal=True)
    assert queue.find(1, 100)["blocked"] is True


def test_a_parked_match_does_not_hold_up_the_queue():
    queue = _queue()
    for _ in range(state_module().BUG_RETRY_LIMIT):
        queue.defer(1, 100, "an unexpected processing error", fatal=True)
    queue.find(1, 100)["next_attempt_at"] = 0
    eligible = [item["match_id"] for item in queue.eligible()]
    assert 100 not in eligible
    assert 101 in eligible


def test_deferring_an_unknown_match_is_a_no_op():
    queue = _queue()
    queue.defer(1, 999, "a transient service failure")
    assert queue.find(1, 999) is None


def test_state_round_trips_and_a_restart_releases_parked_matches():
    MatchState = state_module().MatchState
    queue = _queue()
    for _ in range(state_module().BUG_RETRY_LIMIT):
        queue.defer(1, 100, "an unexpected processing error", fatal=True)
    queue.save()
    assert not list(queue.path.parent.glob("*.tmp")), "atomic write left a temp file"

    reloaded = MatchState.load(queue.path)
    assert any(item.get("blocked") for item in reloaded.pending)

    released = reloaded.release_blocked()
    assert not any(item.get("blocked") for item in released.pending)
    assert all(item.get("attempts", 0) == 0 for item in released.pending)


def test_the_on_disk_format_is_unchanged():
    # An existing deployment's state file must keep loading.
    import json
    queue = _queue()
    queue.save()
    stored = json.loads(queue.path.read_text(encoding="utf-8"))
    assert sorted(stored) == ["accounts", "pending"]


def test_a_legacy_single_cursor_file_migrates():
    MatchState = state_module().MatchState
    path = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    path.write_text('{"last_processed_match": 555}', encoding="utf-8")
    migrated = MatchState.load(path, tracked_steam_ids=[243689827])
    assert migrated.accounts["243689827"]["last_processed_match"] == 555
    assert migrated.accounts["243689827"]["discovered_through_match"] == 555
    assert migrated.pending == []


def test_a_corrupt_state_file_yields_empty_state():
    MatchState = state_module().MatchState
    path = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    path.write_text("{not json", encoding="utf-8")
    loaded = MatchState.load(path)
    assert loaded.accounts == {} and loaded.pending == []


def test_a_missing_state_file_yields_empty_state():
    MatchState = state_module().MatchState
    path = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    loaded = MatchState.load(path)
    assert loaded.accounts == {} and loaded.pending == []


def test_completing_a_match_drops_it_and_advances_the_cursor():
    queue = _queue()
    queue.complete(1, 100)
    assert queue.find(1, 100) is None
    assert queue.find(1, 101) is not None
    assert queue.accounts["1"]["last_processed_match"] == 100


def test_logic_errors_are_not_classified_as_transient():
    # This is the guard that keeps the two absorbed bugs from returning.
    bot = main_module()
    for kind in (KeyError, TypeError, AttributeError, IndexError, ValueError):
        assert not issubclass(kind, bot.TRANSIENT_ERRORS), kind.__name__


def test_service_failures_are_classified_as_transient():
    bot = main_module()
    for kind in (bot.aiohttp.ClientError, TimeoutError, OSError):
        assert issubclass(kind, bot.TRANSIENT_ERRORS), kind.__name__
