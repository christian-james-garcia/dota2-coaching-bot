import asyncio
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands, tasks

# Configure these values in your hosting environment.
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
LLM_API_KEY = os.environ.get("LLM_API_KEY")  # Google AI Studio key
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # Optional fallback
CHANNEL_ID = 1567594143834702025
TRACKED_STEAM_IDS = [243689827]  # OpenDota 32-bit account IDs
STATE_FILE = Path(os.environ.get("LAST_PROCESSED_MATCH_FILE", "last_processed_match.json"))
HISTORY_PAGE_SIZE = 100
GAME_DATA_REFRESH_SECONDS = 6 * 60 * 60
GAME_DATA_RETRY_SECONDS = 10 * 60
RANKED_SAMPLE_CACHE_SECONDS = 6 * 60 * 60
RANKED_SAMPLE_LIMIT = 100

HERO_NAME_MAP = {}
HERO_UNIT_NAME_MAP = {}
HERO_ABILITY_MAP = {}
HERO_ABILITY_DETAILS_MAP = {}
ITEM_NAME_MAP = {}
ITEM_KEY_NAME_MAP = {}
ITEM_DETAILS_MAP = {}
PATCH_NAME_MAP = {}
CURRENT_PATCH_ID = None
CURRENT_PATCH_NAME = None
GAME_DATA_LAST_REFRESH = 0.0
GAME_DATA_LAST_ATTEMPT = 0.0
RANKED_HERO_SAMPLE_CACHE = {}
RANKED_PERSONAL_SAMPLE_CACHE = {}
MATCH_STATE = {"accounts": {}, "pending": []}
STATE_LOADED = False
WEB_RUNNER = None

intents = discord.Intents.default()
# Message content intent is not needed because this bot defines no prefix commands.


async def handle_ping(request):
    return web.Response(text="Dota 2 Coaching Bot is Online!")


async def start_web_server():
    """Start the health endpoint once during bot startup."""
    global WEB_RUNNER
    if WEB_RUNNER is not None:
        return

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    WEB_RUNNER = runner
    print(f"[SERVER] Web server successfully bound to port {port}", flush=True)


def load_match_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        # Migrate the one-cursor state file made by the earlier version.
        if "last_processed_match" in state and TRACKED_STEAM_IDS:
            old_cursor = state.get("last_processed_match")
            if old_cursor is not None:
                account_id = str(TRACKED_STEAM_IDS[0])
                return {
                    "accounts": {
                        account_id: {
                            "last_processed_match": int(old_cursor),
                            "discovered_through_match": int(old_cursor),
                        }
                    },
                    "pending": [],
                }
        accounts = state.get("accounts", {})
        pending = state.get("pending", [])
        if not isinstance(accounts, dict) or not isinstance(pending, list):
            raise ValueError("state must contain an accounts object and pending list")
        return {"accounts": accounts, "pending": pending}
    except FileNotFoundError:
        return {"accounts": {}, "pending": []}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"[STATE] Could not read {STATE_FILE}: {exc}", flush=True)
        return {"accounts": {}, "pending": []}


def save_match_state() -> None:
    """Persist per-account cursors and queued matches atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temporary_file.write_text(json.dumps(MATCH_STATE), encoding="utf-8")
    os.replace(temporary_file, STATE_FILE)


def defer_pending_match(account_id: int, match_id: int, reason: str) -> None:
    """Back off on a failed match without blocking later matches in the queue."""
    for item in MATCH_STATE["pending"]:
        if int(item.get("account_id", -1)) == account_id and int(item.get("match_id", -1)) == match_id:
            attempts = int(item.get("attempts", 0)) + 1
            delay_seconds = min(3600, 60 * (2 ** min(attempts - 1, 5)))
            item["attempts"] = attempts
            item["next_attempt_at"] = time.time() + delay_seconds
            print(
                f"[RETRY] Match {match_id} deferred for {delay_seconds // 60} minute(s) "
                f"after {reason}; attempt {attempts}.",
                flush=True,
            )
            save_match_state()
            return


async def discover_unseen_matches(session: aiohttp.ClientSession, account_id: int):
    """Queue every listed match newer than this account's saved cursor.

    On first startup, queue only the latest match as requested. Later runs page
    backwards until the cursor is reached, so multiple matches are not skipped.
    """
    account_key = str(account_id)
    account_state = MATCH_STATE["accounts"].setdefault(
        account_key,
        {"last_processed_match": None, "discovered_through_match": None},
    )
    cursor = account_state.get("last_processed_match")
    cursor = int(cursor) if cursor is not None else None
    watermark = account_state.get("discovered_through_match", cursor)
    watermark = int(watermark) if watermark is not None else None
    known = {
        (str(item.get("account_id")), int(item.get("match_id")))
        for item in MATCH_STATE["pending"]
        if isinstance(item, dict) and item.get("account_id") is not None and item.get("match_id") is not None
    }

    offset = 0
    found_watermark = False
    first_page = True
    newest_discovered = watermark
    while True:
        url = f"https://api.opendota.com/api/players/{account_id}/matches"
        # significant=0 includes non-standard modes as well as ranked/turbo.
        params = {"limit": HISTORY_PAGE_SIZE, "offset": offset, "significant": 0}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    print(f"[HISTORY] Rate limited for account {account_id}; retrying next cycle.", flush=True)
                    return
                if resp.status != 200:
                    print(f"[HISTORY] Fetch failed for account {account_id}: HTTP {resp.status}", flush=True)
                    return
                page = await resp.json()
        except Exception as exc:
            print(f"[HISTORY] Fetch error for account {account_id}: {exc}", flush=True)
            return

        if not page:
            break

        # A new installation starts with exactly the latest available match.
        if watermark is None and first_page:
            newest = next((m.get("match_id") for m in page if m.get("match_id") is not None), None)
            if newest is not None:
                MATCH_STATE["pending"].append({"account_id": account_id, "match_id": int(newest)})
                account_state["discovered_through_match"] = int(newest)
                print(f"[HISTORY] First run: queued latest match {newest} for account {account_id}.", flush=True)
            save_match_state()
            return

        for match in page:
            match_id = match.get("match_id")
            if match_id is None:
                continue
            match_id = int(match_id)
            if watermark is not None and match_id <= watermark:
                found_watermark = True
                continue
            key = (account_key, match_id)
            if key not in known:
                MATCH_STATE["pending"].append({"account_id": account_id, "match_id": match_id})
                known.add(key)
            if newest_discovered is None or match_id > newest_discovered:
                newest_discovered = match_id

        save_match_state()
        if found_watermark or len(page) < HISTORY_PAGE_SIZE:
            break
        offset += len(page)
        first_page = False
        # Stay comfortably below OpenDota's unauthenticated request rate limit.
        await asyncio.sleep(1.1)

    if watermark is None and first_page:
        print(f"[HISTORY] No match history available yet for account {account_id}.", flush=True)
    elif found_watermark:
        print(f"[HISTORY] Reached discovery cursor {watermark} for account {account_id}.", flush=True)
    if newest_discovered is not None:
        account_state["discovered_through_match"] = newest_discovered
    save_match_state()


async def load_hero_constants(force: bool = False):
    """Refresh OpenDota's game constants so reports use current patch data."""
    global HERO_NAME_MAP, HERO_UNIT_NAME_MAP, HERO_ABILITY_MAP, HERO_ABILITY_DETAILS_MAP
    global ITEM_NAME_MAP, ITEM_KEY_NAME_MAP, ITEM_DETAILS_MAP, PATCH_NAME_MAP
    global CURRENT_PATCH_ID, CURRENT_PATCH_NAME, GAME_DATA_LAST_REFRESH, GAME_DATA_LAST_ATTEMPT
    now = time.time()
    if not force and now - GAME_DATA_LAST_REFRESH < GAME_DATA_REFRESH_SECONDS:
        return
    if not force and now - GAME_DATA_LAST_ATTEMPT < GAME_DATA_RETRY_SECONDS:
        return
    GAME_DATA_LAST_ATTEMPT = now

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        refreshed_everything = True

        try:
            async with session.get("https://api.opendota.com/api/constants/patch") as resp:
                if resp.status == 200:
                    patches = await resp.json()
                    PATCH_NAME_MAP = {int(p["id"]): p["name"] for p in patches if p.get("id") is not None}
                    if PATCH_NAME_MAP:
                        CURRENT_PATCH_ID = max(PATCH_NAME_MAP)
                        CURRENT_PATCH_NAME = PATCH_NAME_MAP[CURRENT_PATCH_ID]
                        print(f"[PATCH] Current OpenDota game-data patch: {CURRENT_PATCH_NAME} (id {CURRENT_PATCH_ID}).", flush=True)
                    else:
                        refreshed_everything = False
                else:
                    refreshed_everything = False
                    print(f"[PATCH] Failed to fetch patch constants. Status: {resp.status}", flush=True)
        except Exception as exc:
            refreshed_everything = False
            print(f"[PATCH] Exception loading patch constants: {exc}", flush=True)

        try:
            async with session.get("https://api.opendota.com/api/heroes") as resp:
                if resp.status == 200:
                    heroes = await resp.json()
                    HERO_NAME_MAP = {h["id"]: h["localized_name"] for h in heroes}
                    HERO_UNIT_NAME_MAP = {h["id"]: h["name"] for h in heroes}
                    print(f"[HEROES] Loaded {len(HERO_NAME_MAP)} hero constants.", flush=True)
                else:
                    refreshed_everything = False
                    print(f"[HEROES] Failed to fetch hero constants. Status: {resp.status}", flush=True)
        except Exception as exc:
            refreshed_everything = False
            print(f"[HEROES] Exception loading hero constants: {exc}", flush=True)

        try:
            async with session.get("https://api.opendota.com/api/constants/items") as resp:
                if resp.status == 200:
                    items = await resp.json()
                    ITEM_NAME_MAP = {
                        int(item["id"]): item.get("dname", key.replace("_", " ").title())
                        for key, item in items.items()
                        if item.get("id") is not None
                    }
                    ITEM_KEY_NAME_MAP = {
                        key: item.get("dname", key.replace("_", " ").title())
                        for key, item in items.items()
                    }
                    ITEM_DETAILS_MAP = {
                        key: {
                            "name": item.get("dname", key.replace("_", " ").title()),
                            "description": item.get("desc") or " ".join(
                                ability.get("description", "") for ability in item.get("abilities", [])
                                if ability.get("description")
                            ),
                        }
                        for key, item in items.items()
                    }
                    print(f"[ITEMS] Loaded {len(ITEM_NAME_MAP)} item names.", flush=True)
                else:
                    refreshed_everything = False
                    print(f"[ITEMS] Failed to fetch item constants. Status: {resp.status}", flush=True)
        except Exception as exc:
            refreshed_everything = False
            print(f"[ITEMS] Exception loading item constants: {exc}", flush=True)

        try:
            async with session.get("https://api.opendota.com/api/constants/hero_abilities") as hero_resp:
                async with session.get("https://api.opendota.com/api/constants/abilities") as ability_resp:
                    if hero_resp.status == 200 and ability_resp.status == 200:
                        hero_abilities = await hero_resp.json()
                        abilities = await ability_resp.json()
                        ability_map = {}
                        ability_details_map = {}
                        for hero_id, unit_name in HERO_UNIT_NAME_MAP.items():
                            names = hero_abilities.get(unit_name, {}).get("abilities", [])
                            ability_map[HERO_NAME_MAP[hero_id]] = [
                                abilities[name].get("dname", name)
                                for name in names
                                if name in abilities and abilities[name].get("dname")
                            ]
                            ability_details_map[HERO_NAME_MAP[hero_id]] = [
                                {
                                    "name": abilities[name].get("dname", name),
                                    "description": abilities[name].get("desc", ""),
                                }
                                for name in names
                                if name in abilities and abilities[name].get("dname")
                            ]
                        HERO_ABILITY_MAP = ability_map
                        HERO_ABILITY_DETAILS_MAP = ability_details_map
                        print(f"[ABILITIES] Loaded current abilities for {len(HERO_ABILITY_MAP)} heroes.", flush=True)
                    else:
                        refreshed_everything = False
                        print(
                            f"[ABILITIES] Could not load ability constants: "
                            f"hero HTTP {hero_resp.status}, abilities HTTP {ability_resp.status}",
                            flush=True,
                        )
        except Exception as exc:
            refreshed_everything = False
            print(f"[ABILITIES] Exception loading ability constants: {exc}", flush=True)

        if refreshed_everything:
            GAME_DATA_LAST_REFRESH = time.time()
            print(f"[CONSTANTS] Current hero, item, ability, and patch data refreshed.", flush=True)


async def get_ranked_hero_sample(
    session: aiohttp.ClientSession, hero_id: int, patch_id: Optional[int], account_id: Optional[int] = None
) -> dict:
    """Summarize recent public ranked matches for this hero and match patch."""
    if patch_id is None:
        return {"available": False, "reason": "The match patch is missing from OpenDota data."}

    cache_key = (int(hero_id), int(patch_id))
    cached = RANKED_HERO_SAMPLE_CACHE.get(cache_key)
    if cached and time.time() - cached["fetched_at"] < RANKED_SAMPLE_CACHE_SECONDS:
        sample = dict(cached["sample"])
    else:
        url = f"https://api.opendota.com/api/heroes/{hero_id}/matches"
        try:
            async with session.get(
                url,
                params={"limit": RANKED_SAMPLE_LIMIT, "patch": patch_id, "lobby_type": 7},
            ) as resp:
                if resp.status != 200:
                    return {"available": False, "reason": f"OpenDota returned HTTP {resp.status}."}
                matches = await resp.json()
        except Exception as exc:
            print(f"[RANKED] Could not fetch hero {hero_id} ranked sample: {exc}", flush=True)
            return {"available": False, "reason": "OpenDota ranked hero sample is temporarily unavailable."}

        valid_matches = [
            match for match in matches
            if isinstance(match, dict) and match.get("radiant") is not None
            and match.get("radiant_win") is not None
        ]
        wins = sum(bool(match["radiant"]) == bool(match["radiant_win"]) for match in valid_matches)
        average_fields = {}
        for field in ("kills", "deaths", "assists", "duration"):
            values = [match.get(field) for match in valid_matches if isinstance(match.get(field), (int, float))]
            if values:
                average_fields[field] = round(sum(values) / len(values), 1)
        sample = {
            "available": bool(valid_matches),
            "sample_size": len(valid_matches),
            "wins": wins,
            "win_rate_pct": round(wins / len(valid_matches) * 100, 1) if valid_matches else None,
            "averages": average_fields,
            "scope": f"up to {RANKED_SAMPLE_LIMIT} recent public ranked matches for this hero in patch {PATCH_NAME_MAP.get(int(patch_id), patch_id)}",
        }
        RANKED_HERO_SAMPLE_CACHE[cache_key] = {
            "fetched_at": time.time(),
            "sample": {key: value for key, value in sample.items()},
        }

    if account_id is not None and sample.get("available"):
        personal_cache_key = (int(account_id), int(hero_id), int(patch_id))
        personal_cached = RANKED_PERSONAL_SAMPLE_CACHE.get(personal_cache_key)
        if personal_cached and time.time() - personal_cached["fetched_at"] < RANKED_SAMPLE_CACHE_SECONDS:
            sample["tracked_player_sample"] = dict(personal_cached["sample"])
            return sample
        try:
            url = f"https://api.opendota.com/api/players/{account_id}/matches"
            async with session.get(
                url,
                params={
                    "limit": RANKED_SAMPLE_LIMIT,
                    "hero_id": hero_id,
                    "patch": patch_id,
                    "lobby_type": 7,
                },
            ) as resp:
                if resp.status == 200:
                    personal_matches = await resp.json()
                    personal_matches = [
                        match for match in personal_matches
                        if isinstance(match, dict) and match.get("radiant") is not None
                        and match.get("radiant_win") is not None
                    ]
                    personal_wins = sum(
                        bool(match["radiant"]) == bool(match["radiant_win"])
                        for match in personal_matches
                    )
                    personal_sample = {
                        "sample_size": len(personal_matches),
                        "wins": personal_wins,
                        "win_rate_pct": round(personal_wins / len(personal_matches) * 100, 1)
                        if personal_matches else None,
                    }
                    RANKED_PERSONAL_SAMPLE_CACHE[personal_cache_key] = {
                        "fetched_at": time.time(),
                        "sample": personal_sample,
                    }
                    sample["tracked_player_sample"] = personal_sample
        except Exception as exc:
            print(f"[RANKED] Could not fetch tracked player's hero sample: {exc}", flush=True)
    return sample


async def add_ranked_context(session: aiohttp.ClientSession, match_data: dict, account_id: int) -> None:
    """Attach same-patch ranked aggregates for each tracked hero in this match."""
    patch_id = match_data.get("patch")
    context = {}
    for player in match_data.get("players", []):
        if player.get("account_id") not in TRACKED_STEAM_IDS:
            continue
        hero_id = player.get("hero_id")
        if hero_id is None:
            continue
        context[str(hero_id)] = await get_ranked_hero_sample(
            session,
            int(hero_id),
            int(patch_id) if patch_id is not None else None,
            account_id=int(player.get("account_id") or account_id),
        )
    match_data["_ranked_hero_samples"] = context


async def fetch_opendota_match(session: aiohttp.ClientSession, match_id: int):
    url = f"https://api.opendota.com/api/matches/{match_id}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            print(f"[OPENDOTA] Match {match_id} fetch failed with status {resp.status}", flush=True)
    except Exception as exc:
        print(f"[OPENDOTA] Error fetching match {match_id}: {exc}", flush=True)
    return None


def match_has_parsed_player_data(match_data: Optional[dict]) -> bool:
    """Wait for replay-derived fields before asking for item and lane analysis."""
    players = (match_data or {}).get("players") or []
    return bool(players) and any(
        "purchase_log" in player or "lh_ten" in player or "benchmarks" in player
        for player in players
    )


async def fetch_parsed_match(session: aiohttp.ClientSession, match_id: int):
    """Request parsing, then poll with a bounded wait instead of a fixed sleep."""
    request_url = f"https://api.opendota.com/api/request/{match_id}"
    try:
        async with session.post(request_url) as resp:
            print(f"[OPENDOTA] Parse request status: {resp.status}", flush=True)
    except Exception as exc:
        print(f"[OPENDOTA] Parse request failed for {match_id}: {exc}", flush=True)

    for attempt in range(6):
        if attempt:
            await asyncio.sleep(5)
        match_data = await fetch_opendota_match(session, match_id)
        if match_has_parsed_player_data(match_data):
            return match_data
        print(f"[OPENDOTA] Match {match_id} is not parsed yet ({attempt + 1}/6).", flush=True)
    return None


def extract_match_telemetry(match_data: dict):
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
        hero_name = HERO_NAME_MAP.get(hero_id, f"Hero_{hero_id}")
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
                item_name = ITEM_KEY_NAME_MAP.get(item_key, item_key.replace("_", " ").title())
                item_timings[item_name] = f"{purchase.get('time', 0) // 60}m"

        benchmarks = player.get("benchmarks") or {}
        gpm_pct = (benchmarks.get("gold_per_min") or {}).get("pct")
        kills = player.get("kills") or 0
        assists = player.get("assists") or 0
        team_kills = match_data.get("radiant_score" if player.get("isRadiant") else "dire_score") or 0
        final_items = [
            ITEM_NAME_MAP.get(player.get(f"item_{slot}", 0), "Unknown item")
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
            "current_abilities": HERO_ABILITY_MAP.get(hero_name, []),
            "current_ability_details": HERO_ABILITY_DETAILS_MAP.get(hero_name, []),
            "ranked_hero_sample": (match_data.get("_ranked_hero_samples") or {}).get(str(hero_id)),
        }
        all_players.append(player_summary)
        if player.get("account_id") in TRACKED_STEAM_IDS:
            detailed_tracked_players.append(player_summary)

    return radiant_heroes, dire_heroes, detailed_tracked_players, all_players


def find_team_label_errors(content: str, team_by_hero: dict) -> list:
    """Detect explicit hero/team labels that contradict OpenDota's player slots."""
    errors = []
    for hero, actual_team in team_by_hero.items():
        wrong_team = "Dire" if actual_team == "Radiant" else "Radiant"
        escaped_hero = re.escape(hero)
        patterns = (
            rf"\b{escaped_hero}\s*\(\s*{wrong_team}\s*\)",
            rf"\b{wrong_team}\s+{escaped_hero}\b",
            rf"\b{wrong_team}[’']s\s+{escaped_hero}\b",
            rf"\b{escaped_hero}\s+(?:is|was|plays|played)\s+(?:on|for|with)\s+{wrong_team}\b",
        )
        if any(re.search(pattern, content, flags=re.IGNORECASE) for pattern in patterns):
            errors.append(f"{hero} is {actual_team}, not {wrong_team}")
    return errors


def build_verified_match_facts(match_data: dict) -> str:
    """Render source data directly so the model cannot alter match facts."""
    radiant, dire, tracked_players, all_players = extract_match_telemetry(match_data)
    duration = (match_data.get("duration") or 0) // 60
    winner = "Radiant" if match_data.get("radiant_win") else "Dire"
    match_patch_id = match_data.get("patch")
    match_patch = PATCH_NAME_MAP.get(int(match_patch_id), f"OpenDota patch id {match_patch_id}") if match_patch_id is not None else "unavailable"
    current_patch = CURRENT_PATCH_NAME or "unavailable"
    lines = [
        "📊 **VERIFIED OPENDOTA FACTS — NOT AI INTERPRETATION**",
        f"- Result: {winner} won {match_data.get('radiant_score')}-{match_data.get('dire_score')} in {duration} minutes.",
        f"- Match patch: {match_patch}. Current hero/item/ability reference data: patch {current_patch} (OpenDota constants, refreshed periodically).",
        f"- Radiant roster: {', '.join(radiant)}.",
        f"- Dire roster: {', '.join(dire)}.",
    ]

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
            samples.append(f"{minute}m: {leader} +{amount:,}")
        if samples:
            lines.append("- Gold lead samples: " + "; ".join(samples) + ".")

    tracked_names = {player["hero"] for player in tracked_players}
    lines.append("- Player stats, final inventories, and recorded key-item purchase times:")
    for player in all_players:
        tracked_label = " **(tracked player)**" if player["hero"] in tracked_names else ""
        percentile = (
            f", GPM percentile {player['gpm_percentile']}%"
            if player.get("gpm_percentile") is not None else ""
        )
        items = ", ".join(player.get("final_items") or []) or "none listed"
        timings = player.get("key_item_timings") or {}
        timing_text = ", ".join(f"{item} {time}" for item, time in timings.items()) or "none in tracked timing list"
        line = (
            f"  - {player['team']} {player['hero']} ({player['lane']}): "
            f"KDA {player['kda']}; LH {player.get('last_hits')}; denies {player.get('denies')}; "
            f"GPM {player.get('gpm')}{percentile}; XPM {player.get('xpm')}; "
            f"hero damage {player.get('hero_damage')}; tower damage {player.get('tower_damage')}; "
            f"final items: {items}; key timings: {timing_text}.{tracked_label}"
        )
        lines.append(line)

    if tracked_players and all(player.get("cs_at_10") is None for player in tracked_players):
        lines.append("- CS@10: unavailable in the OpenDota match response for the tracked player.")
    for player in tracked_players:
        sample = player.get("ranked_hero_sample") or {}
        if sample.get("available"):
            averages = sample.get("averages", {})
            avg_text = ", ".join(
                f"avg {field} {averages[field]}" for field in ("kills", "deaths", "assists")
                if field in averages
            )
            lines.append(
                f"- Same-hero ranked baseline for {player['hero']}: {sample['sample_size']} recent public ranked matches "
                f"in {PATCH_NAME_MAP.get(int(match_patch_id), match_patch)}; {sample['wins']} wins "
                f"({sample['win_rate_pct']}%); {avg_text or 'K/D/A averages unavailable'}. "
                "This is a hero-level sample, not matched to the player's rank or lane."
            )
            personal = sample.get("tracked_player_sample")
            if personal is not None:
                if personal.get("sample_size"):
                    lines.append(
                        f"- Tracked player's public same-hero ranked matches found in this patch: "
                        f"{personal['sample_size']}; {personal['wins']} wins ({personal['win_rate_pct']}%)."
                    )
                else:
                    lines.append(
                        "- No public tracked-player ranked matches for this hero were returned for this patch; "
                        "this does not establish that none were played."
                    )
        else:
            lines.append(
                f"- Same-patch ranked hero baseline for {player['hero']}: unavailable"
                + (f" ({sample.get('reason')})" if sample.get("reason") else ".")
            )
    lines.append("AI coaching text follows; it contains interpretations, not verified match facts.")
    return "\n".join(lines)


def build_current_item_catalog() -> dict:
    """Return current patch descriptions for a compact set of coaching items."""
    item_keys = (
        "black_king_bar", "linkens_sphere", "lotus_orb", "ghost", "glimmer_cape",
        "force_staff", "hurricane_pike", "aeon_disk", "pipe", "crimson_guard",
        "heavens_halberd", "orchid", "bloodthorn", "scythe_of_vyse", "shivas_guard",
        "nullifier", "spirit_vessel", "rod_of_atos", "cyclone", "manta",
    )
    catalog = {}
    for key in item_keys:
        details = ITEM_DETAILS_MAP.get(key) or {}
        description = " ".join((details.get("description") or "").split())
        if details.get("name") and description:
            catalog[details["name"]] = description[:450]
    return catalog


async def generate_ai_coaching_debrief(match_data: dict) -> Optional[str]:
    radiant_lineup, dire_lineup, tracked_players, all_players = extract_match_telemetry(match_data)
    team_by_hero = {player["hero"]: player["team"] for player in all_players}
    duration_mins = match_data.get("duration", 0) // 60
    winner = "Radiant" if match_data.get("radiant_win") else "Dire"
    current_item_catalog = build_current_item_catalog()
    match_patch_id = match_data.get("patch")
    match_patch_name = (
        PATCH_NAME_MAP.get(int(match_patch_id), f"OpenDota patch id {match_patch_id}")
        if match_patch_id is not None else "unavailable"
    )

    coach_prompt = (
        "You are a concise, evidence-led Dota 2 coach. Analyze the actual match data below. "
        "Use hero mechanics only when you are confident they are current. Never invent spell usage, "
        "lane events, or purchases. Say plainly when a field is unavailable.\n\n"
        f"The match was played on patch {match_patch_name}. Your current-mechanics reference is OpenDota's "
        f"latest constants for patch {CURRENT_PATCH_NAME or 'unavailable'}; use those current abilities and the "
        "item catalog below for current recommendations. Do not claim that current mechanics describe an older "
        "match patch if the two patch versions differ.\n"
        "A separate verified-facts panel is printed from the API response. Do not restate or recalculate "
        "scores, gold samples, all-player statistics, item inventories, or purchase times. In the tracked-player "
        "section only, you may cite the tracked player's K/D/A and the ranked sample averages/win rate needed "
        "to explain the comparison. Do not infer that "
        "an item caused a win, loss, gank, or teamfight unless the supplied data records that event.\n"
        "The current ability names and descriptions in each player's telemetry are authoritative. "
        "The current item catalog descriptions are authoritative. Do not name abilities absent from the roster "
        "data, invent item effects, or invent Aghanim's Scepter/Shard upgrade effects. If a recommendation "
        "cannot be supported by these current descriptions, say you are uncertain.\n"
        "TEAM ASSIGNMENTS ARE AUTHORITATIVE. Never place a hero on the other team. "
        "Only describe cross-team matchups between these rosters.\n"
        f"RADIANT ROSTER: {', '.join(radiant_lineup)}\n"
        f"DIRE ROSTER: {', '.join(dire_lineup)}\n\n"
        f"MATCH CONTEXT:\n- Duration: {duration_mins} minutes | Winner: {winner}\n"
        f"- Radiant lineup: {', '.join(radiant_lineup)}\n"
        f"- Dire lineup: {', '.join(dire_lineup)}\n"
        f"- All player telemetry (final named items and key purchase timings included): {json.dumps(all_players, indent=2)}\n"
        f"- Tracked player telemetry: {json.dumps(tracked_players, indent=2)}\n\n"
        f"- Current patch item catalog (item name: current effect): {json.dumps(current_item_catalog, ensure_ascii=False)}\n\n"
        "Structure your response with these sections:\n"
        "🎯 **1. DRAFT MATCHUP** — Give one likely win condition per team as a hypothesis. Do not claim this is what actually happened in-game.\n"
        "🛡️ **2. COUNTER ITEMS** — Give up to 2 specific recommendations with buyer hero, enemy target, and a current, accurate reason. Never invent an ability or Aghanim's upgrade.\n"
        "⚔️ **3. TRACKED PLAYER** — Use the same-hero, same-patch ranked sample in the verified facts to compare the tracked match's K/D/A with the sample averages and win rate. State the sample size and that it is not rank- or lane-matched. Explain one useful lesson from the comparison without treating one match as proof of skill. If the sample is unavailable, say so and teach from this match's supported telemetry only. Do not contradict the verified panel.\n"
        "🎒 **4. BUILD REVIEW** — Give one itemization lesson based on the recorded final inventory. Do not repeat purchase times or claim an item caused a specific fight or win.\n"
        "🗺️ **5. NEXT MATCH DRILL** — Give one measurable practice goal tied to a supported coaching point, not an unobserved event.\n"
    )

    timeout = aiohttp.ClientTimeout(total=45)
    team_validation_failed = False
    async with aiohttp.ClientSession(timeout=timeout) as session:
        providers = []
        if LLM_API_KEY:
            providers.append((
                "GEMINI",
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                ("gemini-3.8-flash", "gemini-3.5-flash-lite"),
            ))
        if GROQ_API_KEY:
            providers.append((
                "GROQ",
                "https://api.groq.com/openai/v1/chat/completions",
                {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                ("openai/gpt-oss-120b",),
            ))

        async def request_completion(provider_name: str, url: str, headers: dict, model_slug: str, prompt: str):
            payload = {
                "model": model_slug,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2500,
            }
            try:
                print(f"[{provider_name}] Requesting completion with model: {model_slug}...", flush=True)
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        choices = result.get("choices", [])
                        if choices:
                            content = choices[0].get("message", {}).get("content")
                            if content and len(content.strip()) > 50:
                                return content
                    else:
                        error_body = (await resp.text())[:500]
                        print(f"[{provider_name}] Model {model_slug} status ({resp.status}): {error_body}", flush=True)
            except Exception as exc:
                print(f"[{provider_name}] Request failed for {model_slug}: {exc}", flush=True)
            return None

        for provider_name, url, headers, models in providers:
            for model_slug in models:
                content = await request_completion(provider_name, url, headers, model_slug, coach_prompt)
                if not content:
                    continue

                errors = find_team_label_errors(content, team_by_hero)
                if errors:
                    correction_prompt = (
                        f"{coach_prompt}\n\n"
                        "Your previous draft assigned a hero to the wrong team. Rewrite the complete response "
                        "using the verified rosters below, and make every matchup cross-team.\n"
                        f"Verified Radiant: {', '.join(radiant_lineup)}\n"
                        f"Verified Dire: {', '.join(dire_lineup)}\n"
                        f"Detected error(s): {'; '.join(errors)}\n"
                        f"Previous draft:\n{content}"
                    )
                    corrected = await request_completion(
                        provider_name, url, headers, model_slug, correction_prompt
                    )
                    if corrected:
                        content = corrected
                        errors = find_team_label_errors(content, team_by_hero)
                    if errors:
                        team_validation_failed = True
                        print(f"[{provider_name}] Team attribution failed validation: {'; '.join(errors)}", flush=True)
                        continue

                print(f"[{provider_name}] Generated a team-validated debrief with {model_slug}.", flush=True)
                return content

    if not LLM_API_KEY and not GROQ_API_KEY:
        print("[AI] No LLM_API_KEY or GROQ_API_KEY is configured.", flush=True)
    if team_validation_failed:
        return "⚠️ AI coaching text withheld because team assignments failed validation. See the verified facts above."
    return None


def chunk_debrief(text: str, max_len: int = 3800) -> list:
    """Split text into chunks that stay below Discord's embed description limit."""
    if not text:
        return []

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        pieces = []
        remaining = paragraph
        while len(remaining) > max_len:
            split_at = max(remaining.rfind("\n", 0, max_len), remaining.rfind(" ", 0, max_len))
            if split_at < max_len // 2:
                split_at = max_len
            pieces.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        pieces.append(remaining)

        for piece in pieces:
            addition = piece if not current else "\n\n" + piece
            if len(current) + len(addition) > max_len:
                if current:
                    chunks.append(current)
                current = piece
            else:
                current += addition
    if current:
        chunks.append(current)
    return chunks


@tasks.loop(minutes=3)
async def check_recent_matches():
    if not TRACKED_STEAM_IDS:
        return
    await load_hero_constants()
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for account_id in TRACKED_STEAM_IDS:
                await discover_unseen_matches(session, account_id)

            if not MATCH_STATE["pending"]:
                return

            now = time.time()
            eligible = [
                item for item in MATCH_STATE["pending"]
                if float(item.get("next_attempt_at", 0)) <= now
            ]
            if not eligible:
                return

            # Match IDs are chronological; process the oldest eligible match first.
            pending = min(eligible, key=lambda item: int(item["match_id"]))
            account_id = int(pending["account_id"])
            match_id = int(pending["match_id"])
            print(f"[QUEUE] Processing match {match_id} for account {account_id}.", flush=True)

            channel = bot.get_channel(CHANNEL_ID)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(CHANNEL_ID)
                except discord.DiscordException as exc:
                    print(f"[DISCORD] Could not access channel {CHANNEL_ID}: {exc}", flush=True)
                    defer_pending_match(account_id, match_id, "Discord channel unavailable")
                    return

            match_data = await fetch_parsed_match(session, match_id)
            if not match_data:
                print(f"[RETRY] Match {match_id} is not ready; will retry next cycle.", flush=True)
                defer_pending_match(account_id, match_id, "OpenDota match data unavailable")
                return

            await add_ranked_context(session, match_data, account_id)

            debrief_text = await generate_ai_coaching_debrief(match_data)
            if not debrief_text:
                print(f"[RETRY] AI providers failed for match {match_id}; will retry next cycle.", flush=True)
                defer_pending_match(account_id, match_id, "AI providers unavailable")
                return

            verified_facts = build_verified_match_facts(match_data)
            complete_debrief = f"{verified_facts}\n\n---\n\n{debrief_text}"
            text_parts = chunk_debrief(complete_debrief)
            total_parts = len(text_parts)
            for idx, part in enumerate(text_parts, 1):
                title = f"🧠 Dota 2 AI Debrief — Match {match_id}"
                if total_parts > 1:
                    title += f" (Part {idx}/{total_parts})"
                embed = discord.Embed(
                    title=title,
                    description=part,
                    color=discord.Color.purple(),
                )
                embed.set_footer(text=f"OpenDota Telemetry Engine | Part {idx} of {total_parts}")
                await channel.send(embed=embed)

            # Remove only after every part was accepted by Discord. The queue is
            # durable, so parse/provider/send failures leave the match for retry.
            MATCH_STATE["pending"] = [
                item for item in MATCH_STATE["pending"]
                if not (
                    int(item["account_id"]) == account_id
                    and int(item["match_id"]) == match_id
                )
            ]
            MATCH_STATE["accounts"].setdefault(str(account_id), {})[
                "last_processed_match"
            ] = match_id
            save_match_state()
            print(f"[QUEUE] Successfully delivered match {match_id}.", flush=True)
    except Exception as exc:
        print(f"[POLL ERROR] Exception during match polling: {exc}", flush=True)
        traceback.print_exc()
        if "account_id" in locals() and "match_id" in locals():
            defer_pending_match(account_id, match_id, "unexpected processing error")


class CoachingBot(commands.Bot):
    async def setup_hook(self):
        # setup_hook runs once per bot process, unlike on_ready which can recur.
        await load_hero_constants()
        try:
            await start_web_server()
        except OSError as exc:
            print(f"[SERVER] Could not start health endpoint: {exc}", flush=True)


bot = CoachingBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    global MATCH_STATE, STATE_LOADED
    print(f"[BOT] Logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    if not STATE_LOADED:
        MATCH_STATE = load_match_state()
        STATE_LOADED = True
    if not check_recent_matches.is_running():
        check_recent_matches.start()
        print("[BOT] Match tracking loop started.", flush=True)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is missing!")
    if not LLM_API_KEY and not GROQ_API_KEY:
        print("[CONFIG] Neither LLM_API_KEY nor GROQ_API_KEY is set; AI reports will retry without posting.", flush=True)
    bot.run(DISCORD_BOT_TOKEN)
