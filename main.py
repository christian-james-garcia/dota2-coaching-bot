import asyncio
import json
import os
import re
import time
import traceback
from collections import Counter, defaultdict
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
RANKED_DETAIL_SAMPLE_LIMIT = 10
RANKED_DETAIL_CANDIDATE_LIMIT = 20
RANKED_MATCH_SCAN_PAGE_SIZE = 100
RANKED_MATCH_SCAN_MAX_PAGES = 12
RETRY_BACKOFF_BASE_SECONDS = 60
RETRY_BACKOFF_MAX_SECONDS = 32 * 60  # the base doubling five times

HERO_NAME_MAP = {}
HERO_UNIT_NAME_MAP = {}
HERO_ABILITY_MAP = {}
HERO_ABILITY_DETAILS_MAP = {}
ABILITY_NAME_MAP = {}
ITEM_NAME_MAP = {}
ITEM_KEY_NAME_MAP = {}
ITEM_DETAILS_MAP = {}
PATCH_NAME_MAP = {}
CURRENT_PATCH_ID = None
CURRENT_PATCH_NAME = None
GAME_DATA_LAST_REFRESH = 0.0
GAME_DATA_LAST_ATTEMPT = 0.0
RANKED_HERO_SAMPLE_CACHE = {}
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
            delay_seconds = min(
                RETRY_BACKOFF_MAX_SECONDS,
                RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)),
            )
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
    global HERO_NAME_MAP, HERO_UNIT_NAME_MAP, HERO_ABILITY_MAP, HERO_ABILITY_DETAILS_MAP, ABILITY_NAME_MAP
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
                        ABILITY_NAME_MAP = {
                            ability_key: ability.get("dname", ability_key)
                            for ability_key, ability in abilities.items()
                            if ability.get("dname")
                        }
                        ability_map = {}
                        ability_details_map = {}
                        for hero_id, unit_name in HERO_UNIT_NAME_MAP.items():
                            raw_names = hero_abilities.get(unit_name, {}).get("abilities", [])
                            # OpenDota nests a list here for heroes with a second
                            # form (Monkey King). An unflattened list is unhashable,
                            # so the `name in abilities` test below used to raise and
                            # abort the whole loop, losing ability data for every hero.
                            names = []
                            for entry in raw_names:
                                if isinstance(entry, list):
                                    names.extend(entry)
                                else:
                                    names.append(entry)
                            names = [name for name in names if isinstance(name, str)]
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
    session: aiohttp.ClientSession, hero_id: int, patch_id: Optional[int]
) -> dict:
    """Aggregate public ranked matches for this hero, explicitly verifying lobby and patch."""
    if patch_id is None:
        return {"available": False, "reason": "The match patch is missing from OpenDota data."}

    cache_key = (int(hero_id), int(patch_id))
    cached = RANKED_HERO_SAMPLE_CACHE.get(cache_key)
    if cached and time.time() - cached["fetched_at"] < RANKED_SAMPLE_CACHE_SECONDS:
        return dict(cached["sample"])

    # /heroes/{id}/matches does not reliably apply the lobby_type filter. Use
    # OpenDota's public match feed, then verify lobby_type and patch on each detail.
    candidates = []
    cursor = None
    try:
        for _ in range(RANKED_MATCH_SCAN_MAX_PAGES):
            params = {"less_than_match_id": cursor} if cursor is not None else None
            async with session.get("https://api.opendota.com/api/publicMatches", params=params) as resp:
                if resp.status != 200:
                    reason = f"OpenDota public match feed returned HTTP {resp.status}."
                    return {"available": False, "reason": reason}
                page = await resp.json()
            if not page:
                break
            for match in page:
                if match.get("lobby_type") != 7:
                    continue
                radiant_team = match.get("radiant_team") or []
                dire_team = match.get("dire_team") or []
                if hero_id in radiant_team or hero_id in dire_team:
                    candidates.append(match)
                    if len(candidates) >= RANKED_DETAIL_CANDIDATE_LIMIT:
                        break
            cursor = page[-1].get("match_id")
            if len(candidates) >= RANKED_DETAIL_CANDIDATE_LIMIT or not cursor or len(page) < RANKED_MATCH_SCAN_PAGE_SIZE:
                break
    except Exception as exc:
        print(f"[RANKED] Could not scan public matches for hero {hero_id}: {exc}", flush=True)
        return {"available": False, "reason": "OpenDota public ranked match data is temporarily unavailable."}

    role_names = {1: "Safe lane", 2: "Mid lane", 3: "Off lane", 4: "Jungle", 5: "Roaming"}
    records = []
    for match in candidates:
        match_id = match.get("match_id")
        try:
            async with session.get(f"https://api.opendota.com/api/matches/{match_id}") as resp:
                if resp.status != 200:
                    continue
                detail = await resp.json()
        except Exception as exc:
            print(f"[RANKED] Could not fetch public ranked match {match_id}: {exc}", flush=True)
            continue

        if detail.get("lobby_type") != 7 or detail.get("patch") != int(patch_id):
            continue
        hero_player = next(
            (player for player in detail.get("players", []) if player.get("hero_id") == int(hero_id)),
            None,
        )
        if not hero_player or match.get("radiant_win") is None:
            continue
        on_radiant = int(hero_id) in (match.get("radiant_team") or [])
        records.append({
            "won": on_radiant == bool(match["radiant_win"]),
            "lane_role": role_names.get(hero_player.get("lane_role")),
            "stats": {
                field: hero_player.get(field)
                for field in ("kills", "deaths", "assists", "gold_per_min", "xp_per_min", "last_hits", "hero_damage", "tower_damage", "duration")
                if isinstance(hero_player.get(field), (int, float))
            },
            "duration": detail.get("duration") or match.get("duration"),
            "final_items": [
                ITEM_NAME_MAP.get(hero_player.get(f"item_{slot}"), "Unknown item")
                for slot in range(6)
                if hero_player.get(f"item_{slot}")
            ],
        })
        if len(records) >= RANKED_DETAIL_SAMPLE_LIMIT:
            break

    def average_stats(rows: list) -> dict:
        fields = ("gold_per_min", "xp_per_min", "last_hits", "hero_damage", "tower_damage")
        averages = {}
        for field in fields:
            values = [row["stats"][field] for row in rows if field in row["stats"]]
            if values:
                averages[field] = round(sum(values) / len(values), 1)
        return averages

    def rate_stats(rows: list) -> dict:
        rates = {}
        for field in ("kills", "deaths", "assists"):
            valid_rows = [
                row for row in rows
                if field in row["stats"] and isinstance(row.get("duration"), (int, float)) and row["duration"] > 0
            ]
            minutes = sum(row["duration"] for row in valid_rows) / 60
            if minutes:
                rates[f"{field}_per_10m"] = round(
                    sum(row["stats"][field] for row in valid_rows) / minutes * 10, 2
                )
        valid_lh_rows = [
            row for row in rows
            if "last_hits" in row["stats"] and isinstance(row.get("duration"), (int, float)) and row["duration"] > 0
        ]
        lh_minutes = sum(row["duration"] for row in valid_lh_rows) / 60
        if lh_minutes:
            rates["last_hits_per_min"] = round(
                sum(row["stats"]["last_hits"] for row in valid_lh_rows) / lh_minutes, 2
            )
        return rates

    wins = sum(record["won"] for record in records)
    role_groups = defaultdict(list)
    item_counts = Counter()
    for record in records:
        if record.get("lane_role"):
            role_groups[record["lane_role"]].append(record)
        item_counts.update(
            item for item in set(record.get("final_items", [])) if item != "Unknown item"
        )

    sample = {
        "available": bool(records),
        "sample_size": len(records),
        "wins": wins,
        "win_rate_pct": round(wins / len(records) * 100, 1) if records else None,
        "averages": average_stats(records),
        "rates": rate_stats(records),
        "lane_role_counts": {role: len(rows) for role, rows in role_groups.items()},
        "by_lane_role": {
            role: {
                "sample_size": len(rows),
                "wins": sum(row["won"] for row in rows),
                "win_rate_pct": round(sum(row["won"] for row in rows) / len(rows) * 100, 1),
                "averages": average_stats(rows),
                "rates": rate_stats(rows),
            }
            for role, rows in role_groups.items() if len(rows) >= 3
        },
        "common_final_items": [
            {"name": item, "matches": count, "sample_rate_pct": round(count / len(records) * 100, 1)}
            for item, count in item_counts.most_common(6)
        ] if records else [],
        "scope": f"up to {RANKED_DETAIL_SAMPLE_LIMIT} recent public ranked matches for this hero in patch {PATCH_NAME_MAP.get(int(patch_id), patch_id)}",
    }
    RANKED_HERO_SAMPLE_CACHE[cache_key] = {"fetched_at": time.time(), "sample": sample}
    return dict(sample)


async def add_ranked_context(session: aiohttp.ClientSession, match_data: dict) -> None:
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


def summarize_teamfights(match_data: dict) -> dict:
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
                "hero": HERO_NAME_MAP.get(player.get("hero_id"), f"Hero_{player.get('hero_id')}"),
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
            total["tracked_player"] = total["tracked_player"] or row["account_id"] in TRACKED_STEAM_IDS
            total["ability_uses"].update({
                ABILITY_NAME_MAP.get(key, key): int(count or 0)
                for key, count in row["ability_uses"].items()
            })
            total["item_uses"].update({
                ITEM_KEY_NAME_MAP.get(key, key.replace("_", " ").title()): int(count or 0)
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


def build_match_analysis(match_data: dict) -> dict:
    """Derive telemetry and teamfight data once so each match is parsed a single time.

    Both the verified panel and the coaching prompt need the same extraction. Parsing
    per consumer meant summarize_teamfights ran twice over the teamfight log, and left
    two call sites that had to stay in agreement.
    """
    radiant, dire, tracked_players, all_players = extract_match_telemetry(match_data)
    return {
        "radiant": radiant,
        "dire": dire,
        "tracked_players": tracked_players,
        "all_players": all_players,
        "teamfights": summarize_teamfights(match_data),
    }


def build_verified_match_facts(match_data: dict, analysis: dict) -> str:
    """Render source data directly so the model cannot alter match facts."""
    radiant = analysis["radiant"]
    dire = analysis["dire"]
    tracked_players = analysis["tracked_players"]
    all_players = analysis["all_players"]
    teamfight_summary = analysis["teamfights"]
    duration = (match_data.get("duration") or 0) // 60
    winner = "Radiant" if match_data.get("radiant_win") else "Dire"
    match_patch_id = match_data.get("patch")
    match_patch = PATCH_NAME_MAP.get(int(match_patch_id), f"OpenDota patch id {match_patch_id}") if match_patch_id is not None else "unavailable"
    current_patch = CURRENT_PATCH_NAME or "unavailable"
    lines = [
        "✨ **MATCH HIGHLIGHTS**",
        f"- **Result:** {winner} **{match_data.get('radiant_score')}-{match_data.get('dire_score')}** in **{duration} min**.",
    ]

    if tracked_players and teamfight_summary.get("full_5v5_count"):
        tracked_team = tracked_players[0]["team"]
        team_stats = teamfight_summary["teams"][tracked_team]
        enemy_team = "Dire" if tracked_team == "Radiant" else "Radiant"
        enemy_stats = teamfight_summary["teams"][enemy_team]
        lines.append(
            f"- **Team 5v5s:** {tracked_team} **{team_stats['wins']}W–{team_stats['losses']}L** "
            f"by death differential across **{teamfight_summary['full_5v5_count']} full fights**; "
            f"team damage {team_stats['damage']:,} vs {enemy_stats['damage']:,}."
        )
    elif not teamfight_summary.get("available"):
        lines.append("- **Team 5v5s:** OpenDota teamfight data unavailable.")

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
            lines.append(
                f"- **{player['hero']} vs public ranked {baseline_scope} sample:** "
                f"{'; '.join(hero_highlights)} (**n={comparison['sample_size']}**; same patch)."
            )
        break

    lines.extend([
        "",
        "📊 **VERIFIED OPENDOTA FACTS — NOT AI INTERPRETATION**",
        f"- **Result:** {winner} won **{match_data.get('radiant_score')}-{match_data.get('dire_score')}** in {duration} minutes.",
        f"- Match patch: **{match_patch}**. Current hero/item/ability reference data: **patch {current_patch}** (OpenDota constants, refreshed periodically).",
        f"- Radiant roster: {', '.join(radiant)}.",
        f"- Dire roster: {', '.join(dire)}.",
    ])

    if not teamfight_summary.get("available"):
        lines.append("- **5v5 teamfight review:** unavailable because OpenDota supplied no usable teamfight events.")
    elif teamfight_summary.get("full_5v5_count"):
        full_count = teamfight_summary["full_5v5_count"]
        lines.append(
            f"- **Teamfight sample:** {teamfight_summary['recorded_engagements']} recorded engagements; "
            f"**{full_count}** had activity from all 10 players. Full 5v5 result is counted by fewer deaths in each event."
        )
        for team in ("Radiant", "Dire"):
            totals = teamfight_summary["teams"][team]
            lines.append(
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
            lines.append(f"    Top recorded damage: **{top_damage}**; most recorded deaths: **{most_deaths}**.")
        tracked_fight_rows = [row for row in teamfight_summary.get("players", []) if row.get("tracked_player")]
        for row in tracked_fight_rows:
            ability_uses = ", ".join(f"{name} {count}" for name, count in row["ability_uses"].items()) or "none recorded"
            item_uses = ", ".join(f"{name} {count}" for name, count in row["item_uses"].items()) or "none recorded"
            lines.append(
                f"- **Tracked player {row['hero']} in full 5v5s:** active in **{row['fights_active']}/{full_count}**; "
                f"damage {row['damage']:,}; deaths {row['deaths']}; recorded ability uses: {ability_uses}; "
                f"recorded item uses: {item_uses}."
            )
    else:
        lines.append(
            f"- Teamfight sample: {teamfight_summary['recorded_engagements']} recorded engagements, "
            "but no complete all-10-player 5v5s were available for a full team comparison."
        )

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
            lines.append("- **Gold lead samples:** " + "; ".join(samples) + ".")

    tracked_names = {player["hero"] for player in tracked_players}
    lines.append("- Player stats, final inventories, and recorded key-item purchase times:")
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
        lines.append(line)

    if tracked_players and all(player.get("cs_at_10") is None for player in tracked_players):
        lines.append("- CS@10: unavailable in the OpenDota match response for the tracked player.")
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
            lines.append(
                f"- **Same-hero ranked baseline for {player['hero']}: {sample['sample_size']} recent public ranked matches "
                f"in {PATCH_NAME_MAP.get(int(match_patch_id), match_patch)}; {sample['wins']} wins "
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
                lines.append(
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
                lines.append(
                    f"- Public same-role {player.get('lane')} baseline for {player['hero']}: "
                    f"{same_role['sample_size']} matches, {same_role['win_rate_pct']}% win rate; "
                    f"{role_stats or 'parsed averages unavailable'}."
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


async def generate_ai_coaching_debrief(match_data: dict, analysis: dict) -> Optional[str]:
    radiant_lineup = analysis["radiant"]
    dire_lineup = analysis["dire"]
    tracked_players = analysis["tracked_players"]
    all_players = analysis["all_players"]
    teamfight_summary = analysis["teamfights"]
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
        "FORMATTING: The verified panel starts with concise match highlights. In your coaching sections, use Discord Markdown bold selectively for the most important team insight, the tracked player's key comparison, the recommended improvement, and the practice drill. Do not bold whole paragraphs or every statistic.\n"
        f"The match was played on patch {match_patch_name}. Your current-mechanics reference is OpenDota's "
        f"latest constants for patch {CURRENT_PATCH_NAME or 'unavailable'}; use those current abilities and the "
        "item catalog below for current recommendations. Do not claim that current mechanics describe an older "
        "match patch if the two patch versions differ.\n"
        "A separate verified-facts panel is printed from the API response. Do not restate or recalculate "
        "scores, gold samples, all-player statistics, item inventories, or purchase times. In the Team 5v5 section, "
        "you may cite only the recorded fight outcomes and teamfight contributions; in the tracked-player "
        "section only, you may cite the tracked player's relevant K/D/A, GPM, XPM, last hits, and the public "
        "ranked baseline metrics/item frequencies needed to explain a comparison. Do not infer that "
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
        f"- OpenDota teamfight summary (recorded participation and outcomes, not positioning or intent): {json.dumps(teamfight_summary, ensure_ascii=False)}\n"
        f"- Current patch item catalog (item name: current effect): {json.dumps(current_item_catalog, ensure_ascii=False)}\n\n"
        "Structure your response with these sections:\n"
        "🎯 **1. DRAFT MATCHUP** — Give one likely win condition per team as a hypothesis. Do not claim this is what actually happened in-game.\n"
        "⚔️ **2. TEAM 5v5 REVIEW** — Focus on the tracked player's team. Use the verified full 5v5 win/loss record, deaths, damage, healing, buybacks, and player contributions. Give one team strength and one team improvement supported by those records. If no complete 5v5 data exists, say so. Do not invent positioning, target selection, communication, spell hits, or objective conversion; offer a review/practice hypothesis rather than claiming an unobserved cause.\n"
        "🛡️ **3. COUNTER ITEMS** — Give up to 2 specific recommendations with buyer hero, enemy target, and a current, accurate reason. Never invent an ability or Aghanim's upgrade.\n"
        "The ranked baseline contains only public ranked games played by this hero, verified as lobby_type 7 and the same patch. It is not the tracked player's personal history. The sample is limited and may be small; do not treat it as a complete or rank-matched meta.\n"
        "📈 **4. TRACKED PLAYER** — Teach from the public ranked hero sample. Compare the tracked match with this hero's public ranked averages, prioritizing same-lane stats only when the verified panel provides at least 3 same-role games. Compare GPM/XPM directly, K/D/A per 10 minutes, and last hits per minute so different match lengths do not distort the comparison. Use common final inventories as descriptive trends only. Identify one evidence-backed improvement area and explain why it follows from the comparison. Treat item frequency and win rate as descriptive, not proof of cause. If sample fields are missing, say so and use only the available comparison. Do not contradict the verified panel.\n"
        "🎒 **5. BUILD REVIEW** — Give one itemization lesson based on the recorded final inventory and current item descriptions. Only say an item was used during a fight if the teamfight item-use telemetry records it. Do not claim an item caused a specific fight or win.\n"
        "🗺️ **6. NEXT MATCH DRILL** — Give one measurable team practice goal based on the 5v5 data, plus a brief individual focus if useful. Do not set a goal based on an unobserved event.\n"
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

            await add_ranked_context(session, match_data)

            analysis = build_match_analysis(match_data)
            debrief_text = await generate_ai_coaching_debrief(match_data, analysis)
            if not debrief_text:
                print(f"[RETRY] AI providers failed for match {match_id}; will retry next cycle.", flush=True)
                defer_pending_match(account_id, match_id, "AI providers unavailable")
                return

            verified_facts = build_verified_match_facts(match_data, analysis)
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
