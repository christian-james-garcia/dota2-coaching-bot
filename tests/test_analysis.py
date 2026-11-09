"""Each match must be parsed once, not once per consumer.

build_verified_match_facts and generate_ai_coaching_debrief both called
extract_match_telemetry and summarize_teamfights on the same match, so every
debrief parsed it twice and left two call sites that had to stay in agreement.
"""
from _support import analysis_module, fixture, game_data, main_module, tracked_ids


def test_analysis_runs_each_helper_once():
    bot = main_module()
    module = analysis_module()
    game = game_data()
    match = fixture("match_parsed.json")
    calls = {"telemetry": 0, "teamfights": 0}
    real_telemetry = module.extract_match_telemetry
    real_teamfights = module.summarize_teamfights

    def counting_telemetry(*args, **kwargs):
        calls["telemetry"] += 1
        return real_telemetry(*args, **kwargs)

    def counting_teamfights(*args, **kwargs):
        calls["teamfights"] += 1
        return real_teamfights(*args, **kwargs)

    module.extract_match_telemetry = counting_telemetry
    module.summarize_teamfights = counting_teamfights
    try:
        analysis = module.build_match_analysis(match, game, tracked_ids(match))
        bot.build_verified_match_facts(match, analysis, game)
    finally:
        module.extract_match_telemetry = real_telemetry
        module.summarize_teamfights = real_teamfights

    assert calls == {"telemetry": 1, "teamfights": 1}


def test_analysis_exposes_every_key_its_consumers_read():
    match = fixture("match_parsed.json")
    analysis = analysis_module().build_match_analysis(match, game_data(), tracked_ids(match))
    assert set(analysis) == {"radiant", "dire", "tracked_players", "all_players", "teamfights"}
    assert len(analysis["radiant"]) == 5
    assert len(analysis["dire"]) == 5
    assert len(analysis["all_players"]) == 10


def test_rosters_are_disjoint():
    match = fixture("match_parsed.json")
    analysis = analysis_module().build_match_analysis(match, game_data(), tracked_ids(match))
    teams = {p["hero"]: p["team"] for p in analysis["all_players"]}
    assert set(teams.values()) == {"Radiant", "Dire"}
    for hero in analysis["radiant"]:
        assert teams[hero] == "Radiant"
    for hero in analysis["dire"]:
        assert teams[hero] == "Dire"


def test_the_panel_renders_from_the_shared_analysis():
    bot = main_module()
    game = game_data()
    match = fixture("match_parsed.json")
    analysis = analysis_module().build_match_analysis(match, game, tracked_ids(match))
    panel = bot.build_verified_match_facts(match, analysis, game)
    assert "VERIFIED OPENDOTA FACTS" in panel
    assert "Radiant roster:" in panel
