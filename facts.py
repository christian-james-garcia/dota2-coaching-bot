"""Rendering the verified panel: match facts printed from the API response.

Nothing in the panel is generated. The model is given this text and told not to
restate or recalculate it, which is the whole basis for the claim that the
numbers in a debrief are real.
"""
from gamedata import GameData


def _highlight_lines(tracked_players, teamfight_summary) -> list:
    """Headline numbers: the tracked team's full-5v5 record and each tracked player's comparison."""
    rows = []
    if tracked_players and teamfight_summary.get("full_5v5_count"):
        tracked_team = tracked_players[0]["team"]
        team_stats = teamfight_summary["teams"][tracked_team]
        enemy_team = "Dire" if tracked_team == "Radiant" else "Radiant"
        enemy_stats = teamfight_summary["teams"][enemy_team]
        rows.append(
            f"- **Team 5v5s:** {tracked_team} **{team_stats['wins']}W–{team_stats['losses']}L** "
            f"by death differential across **{teamfight_summary['full_5v5_count']} full fights**; "
            f"team damage {team_stats['damage']:,} vs {enemy_stats['damage']:,}."
        )
    elif not teamfight_summary.get("available"):
        rows.append("- **Team 5v5s:** OpenDota teamfight data unavailable.")

    for player in tracked_players:
        sample = player.get("ranked_hero_sample") or {}
        if not sample.get("available"):
            continue
        role_sample = sample.get("by_lane_role", {}).get(player.get("lane"))
        comparison = role_sample or sample
        averages = comparison.get("averages", {})
        hero_highlights = []
        for field, label, current_value in (
            ("gold_per_min", "GPM", player.get("gpm")),
            ("xp_per_min", "XPM", player.get("xpm")),
        ):
            baseline_value = averages.get(field)
            if isinstance(current_value, (int, float)) and isinstance(baseline_value, (int, float)):
                hero_highlights.append(f"**{label} {current_value} vs {baseline_value} avg**")
        if hero_highlights:
            baseline_scope = "same-role" if role_sample else "all-role"
            rows.append(
                f"- **{player['hero']} vs public ranked {baseline_scope} sample:** "
                f"{'; '.join(hero_highlights)} (**n={comparison['sample_size']}**; same patch)."
            )
        break
    return rows


def _teamfight_review_lines(teamfight_summary) -> list:
    """Recorded fight outcomes and per-team contributions, or why they are missing."""
    rows = []
    if not teamfight_summary.get("available"):
        rows.append("- **5v5 teamfight review:** unavailable because OpenDota supplied no usable teamfight events.")
    elif teamfight_summary.get("full_5v5_count"):
        full_count = teamfight_summary["full_5v5_count"]
        rows.append(
            f"- **Teamfight sample:** {teamfight_summary['recorded_engagements']} recorded engagements; "
            f"**{full_count}** had activity from all 10 players. Full 5v5 result is counted by fewer deaths in each event."
        )
        for team in ("Radiant", "Dire"):
            totals = teamfight_summary["teams"][team]
            rows.append(
                f"  - **{team}: {totals['wins']} wins, {totals['losses']} losses, {totals['ties']} ties; "
                f"hero kills {totals['kills']}, deaths {totals['deaths']}, damage {totals['damage']:,}, "
                f"healing {totals['healing']:,}, buybacks {totals['buybacks']}.**"
            )
            leaders = teamfight_summary.get("leaders", {}).get(team, {})
            top_damage = ", ".join(
                f"{row['hero']} {row['damage']:,}" for row in leaders.get("top_damage", [])
            ) or "unavailable"
            most_deaths = ", ".join(
                f"{row['hero']} {row['deaths']}" for row in leaders.get("most_deaths", [])
            ) or "unavailable"
            rows.append(f"    Top recorded damage: **{top_damage}**; most recorded deaths: **{most_deaths}**.")
        tracked_fight_rows = [row for row in teamfight_summary.get("players", []) if row.get("tracked_player")]
        for row in tracked_fight_rows:
            ability_uses = ", ".join(f"{name} {count}" for name, count in row["ability_uses"].items()) or "none recorded"
            item_uses = ", ".join(f"{name} {count}" for name, count in row["item_uses"].items()) or "none recorded"
            rows.append(
                f"- **Tracked player {row['hero']} in full 5v5s:** active in **{row['fights_active']}/{full_count}**; "
                f"damage {row['damage']:,}; deaths {row['deaths']}; recorded ability uses: {ability_uses}; "
                f"recorded item uses: {item_uses}."
            )
    else:
        rows.append(
            f"- Teamfight sample: {teamfight_summary['recorded_engagements']} recorded engagements, "
            "but no complete all-10-player 5v5s were available for a full team comparison."
        )
    return rows


def _gold_sample_lines(match_data) -> list:
    """Sampled gold leads, printed so the model never has to compute one."""
    rows = []
    gold_advantage = match_data.get("radiant_gold_adv") or []
    if gold_advantage:
        samples = []
        for minute in (10, 20, 30, 40):
            if minute >= len(gold_advantage):
                continue
            advantage = int(gold_advantage[minute])
            if advantage > 0:
                leader, amount = "Radiant", advantage
            elif advantage < 0:
                leader, amount = "Dire", abs(advantage)
            else:
                leader, amount = "even", 0
            samples.append(f"**{minute}m {leader} +{amount:,}**")
        if samples:
            rows.append("- **Gold lead samples:** " + "; ".join(samples) + ".")
    return rows


def _player_stat_lines(all_players, tracked_players) -> list:
    """Per-player stats, final inventories and recorded key-item purchase times."""
    rows = []
    tracked_names = {player["hero"] for player in tracked_players}
    rows.append("- Player stats, final inventories, and recorded key-item purchase times:")
    for player in all_players:
        is_tracked = player["hero"] in tracked_names
        player_label = (
            f"**{player['team']} {player['hero']} (TRACKED PLAYER)**"
            if is_tracked else f"{player['team']} {player['hero']}"
        )
        percentile = (
            f", GPM percentile {player['gpm_percentile']}%"
            if player.get("gpm_percentile") is not None else ""
        )
        items = ", ".join(player.get("final_items") or []) or "none listed"
        timings = player.get("key_item_timings") or {}
        timing_text = ", ".join(f"{item} {time}" for item, time in timings.items()) or "none in tracked timing list"
        line = (
            f"  - {player_label} ({player['lane']}): "
            f"KDA {'**' if is_tracked else ''}{player['kda']}{'**' if is_tracked else ''}; "
            f"LH {'**' if is_tracked else ''}{player.get('last_hits')}{'**' if is_tracked else ''}; "
            f"denies {player.get('denies')}; "
            f"GPM {'**' if is_tracked else ''}{player.get('gpm')}{'**' if is_tracked else ''}{percentile}; "
            f"XPM {'**' if is_tracked else ''}{player.get('xpm')}{'**' if is_tracked else ''}; "
            f"hero damage {player.get('hero_damage')}; tower damage {player.get('tower_damage')}; "
            f"final items: {items}; key timings: {'**' if is_tracked else ''}{timing_text}{'**' if is_tracked else ''}."
        )
        rows.append(line)
    return rows


def _ranked_baseline_lines(tracked_players, match_data, match_patch, match_patch_id, game) -> list:
    """Same-patch public ranked baselines for each tracked hero."""
    rows = []
    if tracked_players and all(player.get("cs_at_10") is None for player in tracked_players):
        rows.append("- CS@10: unavailable in the OpenDota match response for the tracked player.")
    for player in tracked_players:
        sample = player.get("ranked_hero_sample") or {}
        if sample.get("available"):
            averages = sample.get("averages", {})
            metric_names = {
                "gold_per_min": "GPM", "xp_per_min": "XPM", "last_hits": "LH/game",
                "hero_damage": "hero damage/game",
            }
            avg_text = ", ".join(
                f"avg {metric_names[field]} {averages[field]}"
                for field in metric_names if field in averages
            )
            rates = sample.get("rates", {})
            rate_names = {
                "kills_per_10m": "K/10m", "deaths_per_10m": "D/10m",
                "assists_per_10m": "A/10m", "last_hits_per_min": "LH/min",
            }
            rate_text = ", ".join(
                f"{rate_names[field]} {rates[field]}" for field in rate_names if field in rates
            )
            lane_counts = ", ".join(
                f"{role}: {count}" for role, count in sample.get("lane_role_counts", {}).items()
            ) or "lane role unavailable"
            common_items = ", ".join(
                f"{item['name']} in {item['matches']}/{sample['sample_size']}"
                for item in sample.get("common_final_items", [])
            ) or "item frequency unavailable"
            rows.append(
                f"- **Same-hero ranked baseline for {player['hero']}: {sample['sample_size']} recent public ranked matches "
                f"in {game.patch_names.get(int(match_patch_id), match_patch)}; {sample['wins']} wins "
                f"({sample['win_rate_pct']}%).** {avg_text or 'parsed averages unavailable'}; "
                f"rate-adjusted stats: {rate_text or 'unavailable'}. "
                f"Lane roles: {lane_counts}. Common final inventories: {common_items}. "
                "This is a public hero-level sample, not matched to the tracked player's rank."
            )
            same_role = sample.get("by_lane_role", {}).get(player.get("lane"))
            baseline_averages = same_role.get("averages", {}) if same_role else averages
            baseline_rates = same_role.get("rates", {}) if same_role else rates
            baseline_label = (
                f"same-role public baseline (n={same_role['sample_size']})"
                if same_role else f"all-role public baseline (n={sample['sample_size']})"
            )
            tracked_kda = player.get("kda", "").split("/")
            match_minutes = (match_data.get("duration") or 0) / 60
            tracked_values = {
                "kills_per_10m": round(int(tracked_kda[0]) / match_minutes * 10, 2) if match_minutes and len(tracked_kda) == 3 and tracked_kda[0].isdigit() else None,
                "deaths_per_10m": round(int(tracked_kda[1]) / match_minutes * 10, 2) if match_minutes and len(tracked_kda) == 3 and tracked_kda[1].isdigit() else None,
                "assists_per_10m": round(int(tracked_kda[2]) / match_minutes * 10, 2) if match_minutes and len(tracked_kda) == 3 and tracked_kda[2].isdigit() else None,
                "gold_per_min": player.get("gpm"),
                "xp_per_min": player.get("xpm"),
                "last_hits_per_min": player.get("last_hits") / match_minutes
                if match_minutes and isinstance(player.get("last_hits"), (int, float)) else None,
            }
            if tracked_values["last_hits_per_min"] is not None:
                tracked_values["last_hits_per_min"] = round(tracked_values["last_hits_per_min"], 2)
            comparison_names = {
                "kills_per_10m": "K/10m", "deaths_per_10m": "D/10m",
                "assists_per_10m": "A/10m", "gold_per_min": "GPM",
                "xp_per_min": "XPM", "last_hits_per_min": "LH/min",
            }
            comparisons = []
            for field, name in comparison_names.items():
                current_value = tracked_values.get(field)
                baseline_value = (
                    baseline_rates.get(field) if field in baseline_rates
                    else baseline_averages.get(field)
                )
                if isinstance(current_value, (int, float)) and isinstance(baseline_value, (int, float)):
                    comparisons.append(
                        f"{name} {current_value} vs {baseline_value} avg "
                        f"({current_value - baseline_value:+.1f})"
                    )
            if comparisons:
                rows.append(
                    f"- **Tracked match vs {baseline_label} for {player['hero']}:** "
                    + "; ".join(f"**{comparison}**" for comparison in comparisons) + "."
                )
            if same_role:
                role_averages = same_role.get("averages", {})
                role_rates = same_role.get("rates", {})
                role_stats = ", ".join(
                    f"avg {metric_names[field]} {role_averages[field]}"
                    for field in ("gold_per_min", "xp_per_min", "last_hits", "kills", "deaths", "assists")
                    if field in role_averages
                )
                role_stats += (", " if role_stats and role_rates else "") + ", ".join(
                    f"{rate_names[field]} {role_rates[field]}"
                    for field in rate_names if field in role_rates
                )
                rows.append(
                    f"- Public same-role {player.get('lane')} baseline for {player['hero']}: "
                    f"{same_role['sample_size']} matches, {same_role['win_rate_pct']}% win rate; "
                    f"{role_stats or 'parsed averages unavailable'}."
                )
        else:
            rows.append(
                f"- Same-patch ranked hero baseline for {player['hero']}: unavailable"
                + (f" ({sample.get('reason')})" if sample.get("reason") else ".")
            )
    return rows

def build_verified_match_facts(match_data: dict, analysis: dict, game: GameData) -> str:
    """Render source data directly so the model cannot alter match facts."""
    radiant = analysis["radiant"]
    dire = analysis["dire"]
    tracked_players = analysis["tracked_players"]
    all_players = analysis["all_players"]
    teamfight_summary = analysis["teamfights"]
    duration = (match_data.get("duration") or 0) // 60
    winner = "Radiant" if match_data.get("radiant_win") else "Dire"
    match_patch_id = match_data.get("patch")
    match_patch = game.patch_label(match_patch_id)
    current_patch = game.current_patch_name or "unavailable"
    lines = [
        "✨ **MATCH HIGHLIGHTS**",
        f"- **Result:** {winner} **{match_data.get('radiant_score')}-{match_data.get('dire_score')}** in **{duration} min**.",
    ]

    lines.extend(_highlight_lines(tracked_players, teamfight_summary))

    lines.extend([
        "",
        "📊 **VERIFIED OPENDOTA FACTS — NOT AI INTERPRETATION**",
        f"- **Result:** {winner} won **{match_data.get('radiant_score')}-{match_data.get('dire_score')}** in {duration} minutes.",
        f"- Match patch: **{match_patch}**. Current hero/item/ability reference data: **patch {current_patch}** (OpenDota constants, refreshed periodically).",
        f"- Radiant roster: {', '.join(radiant)}.",
        f"- Dire roster: {', '.join(dire)}.",
    ])

    lines.extend(_teamfight_review_lines(teamfight_summary))

    lines.extend(_gold_sample_lines(match_data))

    lines.extend(_player_stat_lines(all_players, tracked_players))

    lines.extend(_ranked_baseline_lines(tracked_players, match_data, match_patch, match_patch_id, game))
    lines.append("AI coaching text follows; it contains interpretations, not verified match facts.")
    return "\n".join(lines)
