import asyncio
import os
import time
from collections import Counter, defaultdict
from typing import Optional

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands, tasks

from analysis import build_match_analysis
from coaching import generate_ai_coaching_debrief
from facts import build_verified_match_facts
from gamedata import GameData
from reporting import report_unexpected
from state import MatchState
from config import (
    CHANNEL_ID,
    DISCORD_BOT_TOKEN,
    GROQ_API_KEY,
    HISTORY_PAGE_SIZE,
    LLM_API_KEY,
    RANKED_DETAIL_CANDIDATE_LIMIT,
    RANKED_DETAIL_SAMPLE_LIMIT,
    RANKED_MATCH_SCAN_MAX_PAGES,
    RANKED_MATCH_SCAN_PAGE_SIZE,
    RANKED_SAMPLE_CACHE_SECONDS,
    STATE_FILE,
    TRACKED_STEAM_IDS,
    TRANSIENT_ERRORS,
)

RANKED_HERO_SAMPLE_CACHE = {}
STATE = MatchState(STATE_FILE)
STATE_LOADED = False
GAME = GameData()
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


async def discover_unseen_matches(session: aiohttp.ClientSession, account_id: int):
    """Queue every listed match newer than this account's saved cursor.

    On first startup, queue only the latest match as requested. Later runs page
    backwards until the cursor is reached, so multiple matches are not skipped.
    """
    account_key = str(account_id)
    account_state = STATE.account(account_id)
    cursor = account_state.get("last_processed_match")
    cursor = int(cursor) if cursor is not None else None
    watermark = account_state.get("discovered_through_match", cursor)
    watermark = int(watermark) if watermark is not None else None
    known = STATE.queued_keys()

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
        except TRANSIENT_ERRORS as exc:
            print(f"[HISTORY] Fetch error for account {account_id}: {exc}", flush=True)
            return
        except Exception as exc:
            report_unexpected(f"history fetch for account {account_id}", exc)
            return

        if not page:
            break

        # A new installation starts with exactly the latest available match.
        if watermark is None and first_page:
            newest = next((m.get("match_id") for m in page if m.get("match_id") is not None), None)
            if newest is not None:
                STATE.queue(account_id, int(newest))
                account_state["discovered_through_match"] = int(newest)
                print(f"[HISTORY] First run: queued latest match {newest} for account {account_id}.", flush=True)
            STATE.save()
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
                STATE.queue(account_id, match_id)
                known.add(key)
            if newest_discovered is None or match_id > newest_discovered:
                newest_discovered = match_id

        STATE.save()
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
    STATE.save()


async def get_ranked_hero_sample(
    session: aiohttp.ClientSession, hero_id: int, patch_id: Optional[int], game: GameData
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
    except TRANSIENT_ERRORS as exc:
        print(f"[RANKED] Could not scan public matches for hero {hero_id}: {exc}", flush=True)
        return {"available": False, "reason": "OpenDota public ranked match data is temporarily unavailable."}
    except Exception as exc:
        report_unexpected(f"scanning public ranked matches for hero {hero_id}", exc)
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
        except TRANSIENT_ERRORS as exc:
            print(f"[RANKED] Could not fetch public ranked match {match_id}: {exc}", flush=True)
            continue
        except Exception as exc:
            report_unexpected(f"reading public ranked match {match_id}", exc)
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
                game.item_names.get(hero_player.get(f"item_{slot}"), "Unknown item")
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
        "scope": f"up to {RANKED_DETAIL_SAMPLE_LIMIT} recent public ranked matches for this hero in patch {game.patch_names.get(int(patch_id), patch_id)}",
    }
    RANKED_HERO_SAMPLE_CACHE[cache_key] = {"fetched_at": time.time(), "sample": sample}
    return dict(sample)


async def add_ranked_context(session: aiohttp.ClientSession, match_data: dict, game: GameData) -> None:
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
            game,
        )
    match_data["_ranked_hero_samples"] = context


async def fetch_opendota_match(session: aiohttp.ClientSession, match_id: int):
    url = f"https://api.opendota.com/api/matches/{match_id}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            print(f"[OPENDOTA] Match {match_id} fetch failed with status {resp.status}", flush=True)
    except TRANSIENT_ERRORS as exc:
        print(f"[OPENDOTA] Error fetching match {match_id}: {exc}", flush=True)
    except Exception as exc:
        report_unexpected(f"fetching match {match_id}", exc)
    return None


def match_has_parsed_player_data(match_data: Optional[dict]) -> bool:
    """Wait for replay-derived fields before asking for item and lane analysis.

    `version` is set only once OpenDota has parsed the replay, so it is the
    authoritative marker. `benchmarks` is not one: it is present, with null
    percentiles, on matches that have never been parsed. Testing for its
    presence accepted every unparsed match, which made the retry loop in
    fetch_parsed_match pointless and let debriefs be built from empty data.
    """
    match_data = match_data or {}
    if match_data.get("version") is None:
        return False
    players = match_data.get("players") or []
    return bool(players) and any(
        player.get("purchase_log") or player.get("lh_ten") is not None
        for player in players
    )


async def fetch_parsed_match(session: aiohttp.ClientSession, match_id: int):
    """Request parsing, then poll with a bounded wait instead of a fixed sleep."""
    request_url = f"https://api.opendota.com/api/request/{match_id}"
    try:
        async with session.post(request_url) as resp:
            print(f"[OPENDOTA] Parse request status: {resp.status}", flush=True)
    except TRANSIENT_ERRORS as exc:
        print(f"[OPENDOTA] Parse request failed for {match_id}: {exc}", flush=True)
    except Exception as exc:
        report_unexpected(f"requesting a parse of match {match_id}", exc)

    for attempt in range(6):
        if attempt:
            await asyncio.sleep(5)
        match_data = await fetch_opendota_match(session, match_id)
        if match_has_parsed_player_data(match_data):
            return match_data
        print(f"[OPENDOTA] Match {match_id} is not parsed yet ({attempt + 1}/6).", flush=True)
    return None














def balanced_split_point(text: str, split_at: int) -> int:
    """Move a split point back until it is not inside a ** ** pair.

    Discord renders a chunk on its own, so a bold marker separated from its
    partner shows up as literal asterisks. The prompt asks for bold on the key
    takeaways, so an unbalanced split disfigures exactly the emphasized text.
    Falls back to the original point when no balanced boundary exists.
    """
    candidate = split_at
    while candidate > 0 and text[:candidate].count("**") % 2:
        previous = max(text.rfind("\n", 0, candidate), text.rfind(" ", 0, candidate))
        if previous <= 0:
            return split_at
        candidate = previous
    return candidate or split_at


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
            split_at = balanced_split_point(remaining, split_at)
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
    await GAME.refresh()
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for account_id in TRACKED_STEAM_IDS:
                await discover_unseen_matches(session, account_id)

            if not STATE.pending:
                return

            eligible = STATE.eligible()
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
                    STATE.defer(account_id, match_id, "Discord channel unavailable")
                    return

            match_data = await fetch_parsed_match(session, match_id)
            if not match_data:
                print(f"[RETRY] Match {match_id} is not ready; will retry next cycle.", flush=True)
                STATE.defer(account_id, match_id, "OpenDota match data unavailable")
                return

            await add_ranked_context(session, match_data, GAME)

            analysis = build_match_analysis(match_data, GAME, TRACKED_STEAM_IDS)
            debrief_text = await generate_ai_coaching_debrief(
                match_data, analysis, GAME, LLM_API_KEY, GROQ_API_KEY
            )
            if not debrief_text:
                print(f"[RETRY] AI providers failed for match {match_id}; will retry next cycle.", flush=True)
                STATE.defer(account_id, match_id, "AI providers unavailable")
                return

            verified_facts = build_verified_match_facts(match_data, analysis, GAME)
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

            STATE.complete(account_id, match_id)
            print(f"[QUEUE] Successfully delivered match {match_id}.", flush=True)
    except (discord.DiscordException,) + TRANSIENT_ERRORS as exc:
        print(f"[POLL] Transient failure during match polling: {exc}", flush=True)
        if "account_id" in locals() and "match_id" in locals():
            STATE.defer(account_id, match_id, "a transient service failure")
    except Exception as exc:
        report_unexpected("processing a queued match", exc)
        if "account_id" in locals() and "match_id" in locals():
            STATE.defer(
                account_id, match_id, "an unexpected processing error", fatal=True
            )


class CoachingBot(commands.Bot):
    async def setup_hook(self):
        # setup_hook runs once per bot process, unlike on_ready which can recur.
        await GAME.refresh()
        try:
            await start_web_server()
        except OSError as exc:
            print(f"[SERVER] Could not start health endpoint: {exc}", flush=True)


bot = CoachingBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    global STATE, STATE_LOADED
    print(f"[BOT] Logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    if not STATE_LOADED:
        STATE = MatchState.load(STATE_FILE, TRACKED_STEAM_IDS).release_blocked()
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