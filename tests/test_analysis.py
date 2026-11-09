"""Each match must be parsed once, not once per consumer.

build_verified_match_facts and generate_ai_coaching_debrief both called
extract_match_telemetry and summarize_teamfights on the same match, so every
debrief parsed it twice and left two call sites that had to stay in agreement.
"""
from _support import fixture, with_constants


def _tracked(match):
    return [p["account_id"] for p in match["players"] if p.get("account_id")][:1]


def test_analysis_runs_each_helper_once():
    bot = with_constants()
    match = fixture("match_parsed.json")
    calls = {"telemetry": 0, "teamfights": 0}
    real_telemetry = bot.extract_match_telemetry
    real_teamfights = bot.summarize_teamfights
    original_tracked = bot.TRACKED_STEAM_IDS

    def counting_telemetry(*args, **kwargs):
        calls["telemetry"] += 1
        return real_telemetry(*args, **kwargs)

    def counting_teamfights(*args, **kwargs):
        calls["teamfights"] += 1
        return real_teamfights(*args, **kwargs)

    bot.extract_match_telemetry = counting_telemetry
    bot.summarize_teamfights = counting_teamfights
    bot.TRACKED_STEAM_IDS = _tracked(match)
    try:
        analysis = bot.build_match_analysis(match)
        bot.build_verified_match_facts(match, analysis)
    finally:
        bot.extract_match_telemetry = real_telemetry
        bot.summarize_teamfights = real_teamfights
        bot.TRACKED_STEAM_IDS = original_tracked

    assert calls == {"telemetry": 1, "teamfights": 1}


def test_analysis_exposes_every_key_its_consumers_read():
    bot = with_constants()
    analysis = bot.build_match_analysis(fixture("match_parsed.json"))
    assert set(analysis) == {"radiant", "dire", "tracked_players", "all_players", "teamfights"}
    assert len(analysis["radiant"]) == 5
    assert len(analysis["dire"]) == 5
    assert len(analysis["all_players"]) == 10


def test_rosters_are_disjoint():
    bot = with_constants()
    analysis = bot.build_match_analysis(fixture("match_parsed.json"))
    teams = {p["hero"]: p["team"] for p in analysis["all_players"]}
    assert set(teams.values()) == {"Radiant", "Dire"}
    for hero in analysis["radiant"]:
        assert teams[hero] == "Radiant"
    for hero in analysis["dire"]:
        assert teams[hero] == "Dire"


def test_the_panel_renders_from_the_shared_analysis():
    bot = with_constants()
    match = fixture("match_parsed.json")
    original = bot.TRACKED_STEAM_IDS
    bot.TRACKED_STEAM_IDS = _tracked(match)
    try:
        panel = bot.build_verified_match_facts(match, bot.build_match_analysis(match))
    finally:
        bot.TRACKED_STEAM_IDS = original
    assert "VERIFIED OPENDOTA FACTS" in panel
    assert "Radiant roster:" in panel
