"""Totals recomputed from the fixture, independently of the code under test.

The other tests assert shape and absence of crashes. These assert arithmetic:
each expectation is derived straight from the fixture JSON here, so if
summarize_teamfights or extract_match_telemetry starts counting something
differently, these disagree rather than quietly following along.
"""
from _support import analysis_module, fixture, game_data, tracked_ids

MATCH = "match_parsed.json"


def _audit(match):
    """Replay the fight log by hand: per-team totals over full 5v5 fights only."""
    roster = match["players"]
    teams = {"Radiant": {"deaths": 0, "damage": 0, "healing": 0, "buybacks": 0, "wins": 0,
                         "losses": 0, "ties": 0},
             "Dire": {"deaths": 0, "damage": 0, "healing": 0, "buybacks": 0, "wins": 0,
                      "losses": 0, "ties": 0}}
    full_fights = 0
    engagements = 0
    active_counts = {}
    for fight in match.get("teamfights") or []:
        entries = fight.get("players") or []
        if len(entries) < len(roster):
            continue
        active = {"Radiant": 0, "Dire": 0}
        totals = {"Radiant": {"deaths": 0, "damage": 0, "healing": 0, "buybacks": 0},
                  "Dire": {"deaths": 0, "damage": 0, "healing": 0, "buybacks": 0}}
        per_player = []
        for index, player in enumerate(roster):
            entry = entries[index] or {}
            team = "Radiant" if player.get("isRadiant") else "Dire"
            deaths = int(entry.get("deaths") or 0)
            damage = int(entry.get("damage") or 0)
            healing = int(entry.get("healing") or 0)
            buybacks = int(entry.get("buybacks") or 0)
            killed = entry.get("killed") or {}
            hero_kills = sum(int(c or 0) for unit, c in killed.items()
                             if unit.startswith("npc_dota_hero_"))
            is_active = bool(deaths or damage or healing or buybacks or hero_kills
                             or (entry.get("ability_uses") or {})
                             or (entry.get("item_uses") or {}))
            if is_active:
                active[team] += 1
            for field, value in (("deaths", deaths), ("damage", damage),
                                 ("healing", healing), ("buybacks", buybacks)):
                totals[team][field] += value
            per_player.append((team, index, is_active))

        if active["Radiant"] and active["Dire"]:
            engagements += 1
        if active["Radiant"] != 5 or active["Dire"] != 5:
            continue

        full_fights += 1
        for team in teams:
            for field in ("deaths", "damage", "healing", "buybacks"):
                teams[team][field] += totals[team][field]
        if totals["Radiant"]["deaths"] < totals["Dire"]["deaths"]:
            teams["Radiant"]["wins"] += 1
            teams["Dire"]["losses"] += 1
        elif totals["Dire"]["deaths"] < totals["Radiant"]["deaths"]:
            teams["Dire"]["wins"] += 1
            teams["Radiant"]["losses"] += 1
        else:
            teams["Radiant"]["ties"] += 1
            teams["Dire"]["ties"] += 1
        for team, index, is_active in per_player:
            if is_active:
                active_counts[index] = active_counts.get(index, 0) + 1
    return teams, full_fights, engagements, active_counts


def _summary():
    match = fixture(MATCH)
    return match, analysis_module().summarize_teamfights(
        match, game_data(), tracked_ids(match)
    )


def test_full_five_v_five_count_matches_a_hand_count():
    match, summary = _summary()
    _, full_fights, _, _ = _audit(match)
    assert summary["full_5v5_count"] == full_fights
    assert full_fights > 0, "the fixture must contain a full 5v5 to be useful here"


def test_recorded_engagements_match_a_hand_count():
    match, summary = _summary()
    _, _, engagements, _ = _audit(match)
    assert summary["recorded_engagements"] == engagements


def test_team_totals_match_a_hand_sum():
    match, summary = _summary()
    expected, _, _, _ = _audit(match)
    for team in ("Radiant", "Dire"):
        for field in ("deaths", "damage", "healing", "buybacks", "wins", "losses", "ties"):
            assert summary["teams"][team][field] == expected[team][field], f"{team}.{field}"


def test_a_teams_kills_are_the_other_teams_deaths():
    _, summary = _summary()
    assert summary["teams"]["Radiant"]["kills"] == summary["teams"]["Dire"]["deaths"]
    assert summary["teams"]["Dire"]["kills"] == summary["teams"]["Radiant"]["deaths"]


def test_wins_and_losses_are_symmetric():
    _, summary = _summary()
    radiant, dire = summary["teams"]["Radiant"], summary["teams"]["Dire"]
    assert radiant["wins"] == dire["losses"]
    assert dire["wins"] == radiant["losses"]
    assert radiant["ties"] == dire["ties"]
    assert radiant["wins"] + radiant["losses"] + radiant["ties"] == summary["full_5v5_count"]


def test_per_player_fight_counts_match_a_hand_count():
    match, summary = _summary()
    _, _, _, active_counts = _audit(match)
    by_hero = {}
    for index, count in active_counts.items():
        player = match["players"][index]
        team = "Radiant" if player.get("isRadiant") else "Dire"
        by_hero[(team, game_data().hero_name(player.get("hero_id")))] = count
    for row in summary["players"]:
        key = (row["team"], row["hero"])
        assert row["fights_active"] == by_hero[key], key


def test_player_damage_never_exceeds_its_team_total():
    _, summary = _summary()
    for row in summary["players"]:
        assert row["damage"] <= summary["teams"][row["team"]]["damage"]


# ---- extract_match_telemetry ----

def _telemetry(match=None, tracked=None):
    match = fixture(MATCH) if match is None else match
    tracked = tracked_ids(match) if tracked is None else tracked
    return match, analysis_module().extract_match_telemetry(match, game_data(), tracked)


def test_rosters_follow_the_is_radiant_flag():
    match, (radiant, dire, _, _) = _telemetry()
    expected_radiant = sum(1 for p in match["players"] if p.get("isRadiant"))
    assert len(radiant) == expected_radiant
    assert len(dire) == len(match["players"]) - expected_radiant


def test_every_player_appears_exactly_once():
    match, (radiant, dire, _, all_players) = _telemetry()
    assert len(all_players) == len(match["players"])
    assert len(radiant) + len(dire) == len(match["players"])


def test_the_tracked_player_is_the_requested_account():
    match, (_, _, tracked_players, _) = _telemetry()
    wanted = tracked_ids(match)[0]
    expected_hero = game_data().hero_name(
        next(p["hero_id"] for p in match["players"] if p.get("account_id") == wanted)
    )
    assert [p["hero"] for p in tracked_players] == [expected_hero]


def test_kda_and_rates_match_the_raw_fields():
    match, (_, _, _, all_players) = _telemetry()
    by_hero = {game_data().hero_name(p["hero_id"]): p for p in match["players"]}
    for row in all_players:
        raw = by_hero[row["hero"]]
        assert row["kda"] == f"{raw.get('kills')}/{raw.get('deaths')}/{raw.get('assists')}"
        assert row["gpm"] == raw.get("gold_per_min")
        assert row["xpm"] == raw.get("xp_per_min")
        assert row["hero_damage"] == raw.get("hero_damage")


def test_an_untracked_account_yields_no_tracked_players():
    _, (_, _, tracked_players, all_players) = _telemetry(tracked=[-1])
    assert tracked_players == []
    assert len(all_players) == 10, "the rest of the match is still described"


def test_anonymous_players_do_not_break_extraction():
    match = fixture(MATCH)
    for player in match["players"]:
        player["account_id"] = None
    _, (_, _, tracked_players, all_players) = _telemetry(match=match, tracked=[123])
    assert tracked_players == []
    assert len(all_players) == 10


def test_an_unparsed_match_degrades_rather_than_raising():
    match = fixture("match_unparsed.json")
    _, (radiant, dire, _, all_players) = _telemetry(match=match, tracked=[])
    assert len(all_players) == len(match["players"])
    # No replay data, so these are absent rather than wrong.
    assert all(row["cs_at_10"] is None for row in all_players)
    assert all(row["gpm_percentile"] is None for row in all_players)
