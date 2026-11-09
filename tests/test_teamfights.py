"""summarize_teamfights must not raise on a match with a full 5v5 fight.

Per-hero totals were stored under the tuple (team, hero) and read back by hero
name alone, so the first active player of the first full 5v5 raised KeyError.
The poll loop absorbed it, deferred the match, and never posted it.
"""
from _support import (fixture, main_module, match_without_full_five_v_fives,
                      match_without_teamfights, with_constants)


def _tracked(match):
    return [p["account_id"] for p in match["players"] if p.get("account_id")][:1]


def test_a_full_five_v_five_does_not_raise():
    bot = with_constants()
    match = fixture("match_parsed.json")
    original = bot.TRACKED_STEAM_IDS
    bot.TRACKED_STEAM_IDS = _tracked(match)
    try:
        summary = bot.summarize_teamfights(match)
    finally:
        bot.TRACKED_STEAM_IDS = original
    assert summary["available"] is True
    assert summary["full_5v5_count"] >= 1
    assert summary["players"], "a full 5v5 should produce per-player rows"


def test_per_player_rows_are_unique_per_team_and_hero():
    bot = with_constants()
    match = fixture("match_parsed.json")
    summary = bot.summarize_teamfights(match)
    seen = [(row["team"], row["hero"]) for row in summary["players"]]
    assert len(seen) == len(set(seen))


def test_an_empty_teamfight_log_reports_unavailable():
    bot = with_constants()
    summary = bot.summarize_teamfights(match_without_teamfights())
    assert summary["available"] is False
    assert "reason" in summary


def test_telemetry_without_a_full_five_v_five_is_available_but_empty():
    bot = with_constants()
    summary = bot.summarize_teamfights(match_without_full_five_v_fives())
    assert summary["available"] is True
    assert summary["full_5v5_count"] == 0
    assert summary["players"] == []
    assert summary["recorded_engagements"] >= 0


def test_a_match_missing_the_teamfight_key_does_not_raise():
    bot = main_module()
    summary = bot.summarize_teamfights({"players": []})
    assert summary["available"] is False
