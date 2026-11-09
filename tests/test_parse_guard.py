"""match_has_parsed_player_data must not accept an unparsed match.

It used to test for a "benchmarks" key, which OpenDota sets on matches it has
never parsed. Every unparsed match passed, so fetch_parsed_match returned on
its first attempt and debriefs were built from empty data.
"""
from _support import fixture, opendota_module


def test_accepts_a_parsed_match():
    assert opendota_module().match_has_parsed_player_data(fixture("match_parsed.json")) is True


def test_rejects_an_unparsed_match():
    assert opendota_module().match_has_parsed_player_data(fixture("match_unparsed.json")) is False


def test_unparsed_match_still_carries_hollow_benchmarks():
    # The reason the old check passed: the key is present, the data is not.
    player = fixture("match_unparsed.json")["players"][0]
    assert "benchmarks" in player
    assert "purchase_log" not in player
    assert player["benchmarks"]["gold_per_min"]["pct"] is None


def test_rejects_missing_and_empty_input():
    guard = opendota_module().match_has_parsed_player_data
    assert guard(None) is False
    assert guard({}) is False
    assert guard({"version": 22, "players": []}) is False


def test_version_alone_is_not_enough():
    guard = opendota_module().match_has_parsed_player_data
    assert guard({"version": 22, "players": [{"benchmarks": {}}]}) is False


def test_replay_fields_alone_are_not_enough():
    guard = opendota_module().match_has_parsed_player_data
    assert guard({"players": [{"purchase_log": [{"key": "blink"}]}]}) is False


def test_zero_last_hits_at_ten_is_real_data():
    # lh_ten == 0 is a legitimate value and must not read as missing.
    guard = opendota_module().match_has_parsed_player_data
    assert guard({"version": 22, "players": [{"lh_ten": 0}]}) is True
