"""summarize_teamfights must not raise on a match with a full 5v5 fight.

Per-hero totals were stored under the tuple (team, hero) and read back by hero
name alone, so the first active player of the first full 5v5 raised KeyError.
The poll loop absorbed it, deferred the match, and never posted it.
"""
from _support import (analysis_module, fixture, game_data,
                      match_without_full_five_v_fives, match_without_teamfights,
                      tracked_ids)


def test_a_full_five_v_five_does_not_raise():
    match = fixture("match_parsed.json")
    summary = analysis_module().summarize_teamfights(
        match, game_data(), tracked_ids(match)
    )
    assert summary["available"] is True
    assert summary["full_5v5_count"] >= 1
    assert summary["players"], "a full 5v5 should produce per-player rows"


def test_per_player_rows_are_unique_per_team_and_hero():
    match = fixture("match_parsed.json")
    summary = analysis_module().summarize_teamfights(match, game_data(), tracked_ids(match))
    seen = [(row["team"], row["hero"]) for row in summary["players"]]
    assert len(seen) == len(set(seen))


def test_an_empty_teamfight_log_reports_unavailable():
    summary = analysis_module().summarize_teamfights(match_without_teamfights(), game_data(), [])
    assert summary["available"] is False
    assert "reason" in summary


def test_telemetry_without_a_full_five_v_five_is_available_but_empty():
    summary = analysis_module().summarize_teamfights(
        match_without_full_five_v_fives(), game_data(), []
    )
    assert summary["available"] is True
    assert summary["full_5v5_count"] == 0
    assert summary["players"] == []
    assert summary["recorded_engagements"] >= 0


def test_a_match_missing_the_teamfight_key_does_not_raise():
    summary = analysis_module().summarize_teamfights({"players": []}, game_data(), [])
    assert summary["available"] is False
