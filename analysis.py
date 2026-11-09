"""Turning an OpenDota match into the numbers a debrief is built from.

Nothing here reads module state: game constants arrive as a GameData and the
tracked accounts as an argument, which is what makes these callable from a test
with a fixture and nothing else.
"""
from collections import Counter

from gamedata import GameData


def extract_match_telemetry(match_data: dict, game: GameData, tracked_ids) -> tuple:
    radiant_heroes = []
    dire_heroes = []
    detailed_tracked_players = []
    all_players = []
    key_items = {
        "black_king_bar", "blink", "manta", "orchid", "bfury", "force_staff",
        "glimmer_cape", "pipe", "silver_edge", "desolator", "vessel",
        "heavens_halberd", "aghanims_scepter", "aghanims_shard", "nullifier",
        "rod_of_atos", "monkey_king_bar", "bloodthorn", "scythe_of_vyse", "skadi",
        "shivas_guard", "assault", "lotus_orb", "aeon_disk", "crimson_guard",
        "hurricane_pike", "linkens_sphere", "refresher", "solar_crest", "ghost",
    }
    lane_roles = {1: "Safe lane", 2: "Mid lane", 3: "Off lane", 4: "Jungle", 5: "Roaming"}

    for player in match_data.get("players", []):
        hero_id = player.get("hero_id")
        hero_name = game.hero_name(hero_id)
        team = "Radiant" if player.get("isRadiant") else "Dire"

        if player.get("isRadiant"):
            radiant_heroes.append(hero_name)
        else:
            dire_heroes.append(hero_name)

        purchase_log = player.get("purchase_log") or []
        item_timings = {}
        for purchase in purchase_log:
            item_key = purchase.get("key")
            if item_key in key_items and item_key not in item_timings:
                item_name = game.item_key_names.get(item_key, item_key.replace("_", " ").title())
                item_timings[item_name] = f"{purchase.get('time', 0) // 60}m"

        benchmarks = player.get("benchmarks") or {}
        gpm_pct = (benchmarks.get("gold_per_min") or {}).get("pct")
        kills = player.get("kills") or 0
        assists = player.get("assists") or 0
        team_kills = match_data.get("radiant_score" if player.get("isRadiant") else "dire_score") or 0
        final_items = [
            game.item_names.get(player.get(f"item_{slot}", 0), "Unknown item")
            for slot in range(6)
            if player.get(f"item_{slot}", 0)
        ]
        player_summary = {
            "hero_id": hero_id,
            "hero": hero_name,
            "team": team,
            "lane": lane_roles.get(player.get("lane_role"), "Unknown"),
            "kda": f"{kills}/{player.get('deaths', 0)}/{assists}",
            "kill_participation_pct": round((kills + assists) / team_kills * 100, 1) if team_kills else None,
            "last_hits": player.get("last_hits"),
            "denies": player.get("denies"),
            "cs_at_10": player.get("lh_ten"),
            "gpm": player.get("gold_per_min"),
            "gpm_percentile": round(gpm_pct * 100, 1) if gpm_pct is not None else None,
            "xpm": player.get("xp_per_min"),
            "hero_damage": player.get("hero_damage"),
            "tower_damage": player.get("tower_damage"),
            "final_items": final_items,
            "key_item_timings": item_timings,
            "current_abilities": game.hero_abilities.get(hero_name, []),
            "current_ability_details": game.hero_ability_details.get(hero_name, []),
            "ranked_hero_sample": (match_data.get("_ranked_hero_samples") or {}).get(str(hero_id)),
        }
        all_players.append(player_summary)
        if player.get("account_id") in tracked_ids:
            detailed_tracked_players.append(player_summary)

    return radiant_heroes, dire_heroes, detailed_tracked_players, all_players


def summarize_teamfights(match_data: dict, game: GameData, tracked_ids) -> dict:
    """Summarize recorded full 5v5s and player contributions without guessing actions."""
    roster = match_data.get("players") or []
    events = match_data.get("teamfights") or []
    if len(roster) != 10 or not events:
        return {"available": False, "reason": "OpenDota teamfight telemetry is unavailable."}

    teams = {
        "Radiant": {"wins": 0, "losses": 0, "ties": 0, "kills": 0, "deaths": 0, "damage": 0, "healing": 0, "buybacks": 0},
        "Dire": {"wins": 0, "losses": 0, "ties": 0, "kills": 0, "deaths": 0, "damage": 0, "healing": 0, "buybacks": 0},
    }
    hero_totals = {}
    recorded_engagements = 0
    full_five_v_fives = 0

    for fight in events:
        fight_players = fight.get("players") or []
        if len(fight_players) < len(roster):
            continue

        active_by_team = {"Radiant": 0, "Dire": 0}
        fight_totals = {
            "Radiant": {"deaths": 0, "damage": 0, "healing": 0, "buybacks": 0},
            "Dire": {"deaths": 0, "damage": 0, "healing": 0, "buybacks": 0},
        }
        player_rows = []
        for index, player in enumerate(roster):
            event_player = fight_players[index] or {}
            team = "Radiant" if player.get("isRadiant") else "Dire"
            deaths = int(event_player.get("deaths") or 0)
            damage = int(event_player.get("damage") or 0)
            healing = int(event_player.get("healing") or 0)
            buybacks = int(event_player.get("buybacks") or 0)
            killed = event_player.get("killed") or {}
            hero_kills = sum(
                int(count or 0) for unit, count in killed.items()
                if unit.startswith("npc_dota_hero_")
            )
            ability_uses = event_player.get("ability_uses") or {}
            item_uses = event_player.get("item_uses") or {}
            active = bool(
                deaths or damage or healing or buybacks or hero_kills or ability_uses or item_uses
            )
            if active:
                active_by_team[team] += 1

            fight_totals[team]["deaths"] += deaths
            fight_totals[team]["damage"] += damage
            fight_totals[team]["healing"] += healing
            fight_totals[team]["buybacks"] += buybacks
            player_rows.append({
                "hero": game.hero_name(player.get("hero_id")),
                "team": team,
                "account_id": player.get("account_id"),
                "active": active,
                "deaths": deaths,
                "damage": damage,
                "healing": healing,
                "buybacks": buybacks,
                "hero_kills": hero_kills,
                "ability_uses": ability_uses,
                "item_uses": item_uses,
            })

        if active_by_team["Radiant"] and active_by_team["Dire"]:
            recorded_engagements += 1
        if active_by_team["Radiant"] != 5 or active_by_team["Dire"] != 5:
            continue

        full_five_v_fives += 1
        radiant_deaths = fight_totals["Radiant"]["deaths"]
        dire_deaths = fight_totals["Dire"]["deaths"]
        if radiant_deaths < dire_deaths:
            teams["Radiant"]["wins"] += 1
            teams["Dire"]["losses"] += 1
        elif dire_deaths < radiant_deaths:
            teams["Dire"]["wins"] += 1
            teams["Radiant"]["losses"] += 1
        else:
            teams["Radiant"]["ties"] += 1
            teams["Dire"]["ties"] += 1

        for team in teams:
            opponent = "Dire" if team == "Radiant" else "Radiant"
            teams[team]["kills"] += fight_totals[opponent]["deaths"]
            for metric in ("deaths", "damage", "healing", "buybacks"):
                teams[team][metric] += fight_totals[team][metric]

        for row in player_rows:
            if not row["active"]:
                continue
            hero_key = (row["team"], row["hero"])
            hero_totals.setdefault(hero_key, {
                "hero": row["hero"], "team": row["team"], "fights_active": 0,
                "deaths": 0, "damage": 0, "healing": 0, "buybacks": 0,
                "hero_kills": 0, "ability_uses": Counter(), "item_uses": Counter(),
                "tracked_player": False,
            })
            total = hero_totals[hero_key]
            total["fights_active"] += 1
            total["deaths"] += row["deaths"]
            total["damage"] += row["damage"]
            total["healing"] += row["healing"]
            total["buybacks"] += row["buybacks"]
            total["hero_kills"] += row["hero_kills"]
            total["tracked_player"] = total["tracked_player"] or row["account_id"] in tracked_ids
            total["ability_uses"].update({
                game.ability_names.get(key, key): int(count or 0)
                for key, count in row["ability_uses"].items()
            })
            total["item_uses"].update({
                game.item_key_names.get(key, key.replace("_", " ").title()): int(count or 0)
                for key, count in row["item_uses"].items()
            })

    if not full_five_v_fives:
        return {
            "available": True,
            "recorded_engagements": recorded_engagements,
            "full_5v5_count": 0,
            "teams": teams,
            "players": [],
        }

    players_summary = []
    for total in hero_totals.values():
        players_summary.append({
            **{key: value for key, value in total.items() if key not in ("ability_uses", "item_uses")},
            "ability_uses": dict(total["ability_uses"].most_common(5)),
            "item_uses": dict(total["item_uses"].most_common(5)),
        })

    leaders = {}
    for team in teams:
        team_players = [row for row in players_summary if row["team"] == team]
        leaders[team] = {
            "top_damage": sorted(team_players, key=lambda row: row["damage"], reverse=True)[:2],
            "most_deaths": sorted(team_players, key=lambda row: row["deaths"], reverse=True)[:2],
        }
    return {
        "available": True,
        "recorded_engagements": recorded_engagements,
        "full_5v5_count": full_five_v_fives,
        "outcome_method": "A teamfight win is determined by which team recorded fewer deaths in that engagement.",
        "teams": teams,
        "leaders": leaders,
        "players": players_summary,
    }


def build_match_analysis(match_data: dict, game: GameData, tracked_ids) -> dict:
    """Derive telemetry and teamfight data once so each match is parsed a single time.

    Both the verified panel and the coaching prompt need the same extraction. Parsing
    per consumer meant summarize_teamfights ran twice over the teamfight log, and left
    two call sites that had to stay in agreement.
    """
    radiant, dire, tracked_players, all_players = extract_match_telemetry(match_data, game, tracked_ids)
    return {
        "radiant": radiant,
        "dire": dire,
        "tracked_players": tracked_players,
        "all_players": all_players,
        "teamfights": summarize_teamfights(match_data, game, tracked_ids),
    }
