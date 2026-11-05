import discord
from discord.ext import commands, tasks
from aiohttp import web
import aiohttp
import asyncio
import json
import os
import traceback

# SECURE CONFIGURATION
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
LLM_API_KEY = os.environ.get("LLM_API_KEY")
CHANNEL_ID = 1567594143834702025
TRACKED_STEAM_IDS = [243689827]

# Reliable free-tier model configuration
OPENROUTER_MODEL = "nvidia/nemotron-3.5-lightning:free"

HERO_NAME_MAP = {}
LAST_PROCESSED_MATCH = None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Health check endpoint for Render
async def handle_ping(request):
    return web.Response(text="Dota 2 Coaching Bot is Online!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[SERVER] Web server successfully bound to port {port}")

async def load_hero_constants():
    global HERO_NAME_MAP
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("https://api.opendota.com/api/heroes") as resp:
                if resp.status == 200:
                    heroes = await resp.json()
                    HERO_NAME_MAP = {h["id"]: h["localized_name"] for h in heroes}
                    print(f"[HEROES] Loaded {len(HERO_NAME_MAP)} hero constants.")
                else:
                    print(f"[HEROES] Failed to fetch hero constants. Status: {resp.status}")
        except Exception as e:
            print(f"[HEROES] Exception loading hero constants: {e}")

async def fetch_opendota_match(match_id: int):
    url = f"https://api.opendota.com/api/matches/{match_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                print(f"[OPENDOTA] Match {match_id} fetch failed with status {resp.status}")
        except Exception as e:
            print(f"[OPENDOTA] Error fetching match {match_id}: {e}")
    return None

def extract_match_telemetry(match_data: dict):
    radiant_heroes = []
    dire_heroes = []
    detailed_tracked_players = []

    for p in match_data.get("players", []):
        hero_name = HERO_NAME_MAP.get(p.get("hero_id"), f"Hero_{p.get('hero_id')}")
        
        if p.get("isRadiant"):
            radiant_heroes.append(hero_name)
        else:
            dire_heroes.append(hero_name)

        if p.get("account_id") in TRACKED_STEAM_IDS:
            purchase_log = p.get("purchase_log", []) or []
            key_items = [
                "black_king_bar", "blink", "manta", "orchid", "bfury", "force_staff", 
                "glimmer_cape", "pipe", "silver_edge", "desolator", "vessel", 
                "heavens_halberd", "aghanims_scepter", "nullifier", "rod_of_atos",
                "monkey_king_bar", "bloodthorn", "scythe_of_vyse", "skadi", "shivas_guard"
            ]
            
            item_timings = {}
            for item in purchase_log:
                item_key = item.get("key")
                if item_key in key_items and item_key not in item_timings:
                    time_mins = item.get("time", 0) // 60
                    item_timings[item_key] = f"{time_mins}m"

            benchmarks = p.get("benchmarks", {})
            gpm_pct = round(benchmarks.get("gold_per_min", {}).get("pct", 0) * 100, 1)

            detailed_tracked_players.append({
                "hero": hero_name,
                "team": "Radiant" if p.get("isRadiant") else "Dire",
                "kda": f"{p.get('kills')}/{p.get('deaths')}/{p.get('assists')}",
                "cs_at_10": p.get("lh_ten", "N/A"),
                "gpm": p.get("gold_per_min"),
                "xpm": p.get("xp_per_min"),
                "gpm_percentile": f"{gpm_pct}%",
                "key_item_timings": item_timings if item_timings else "None tracked",
                "final_items": [HERO_NAME_MAP.get(p.get(f"item_{i}"), "Empty") for i in range(6)],
                "hero_damage": p.get("hero_damage"),
                "tower_damage": p.get("tower_damage")
            })

    return radiant_heroes, dire_heroes, detailed_tracked_players

async def generate_ai_coaching_debrief(match_data: dict) -> str:
    radiant_lineup, dire_lineup, tracked_players = extract_match_telemetry(match_data)
    duration_mins = match_data.get("duration", 0) // 60
    winner = "Radiant" if match_data.get("radiant_win") else "Dire"
    
    coach_prompt = f"""
    You are an elite 7,000+ MMR Dota 2 Coach analyzing a 5-stack team match.
    Provide a deeply tactical post-game analysis covering Draft, Laning, Macro, and explicit ITEM COUNTER BUILDS.

    MATCH SETUP & TELEMETRY:
    - Match Duration: {duration_mins} minutes
    - Winning Team: {winner}
    - Full Radiant Lineup: {", ".join(radiant_lineup)}
    - Full Dire Lineup: {", ".join(dire_lineup)}
    - Our Tracked Players Performance & Items: {json.dumps(tracked_players, indent=2)}

    Please structure your response strictly using the following headers:

    🎯 **1. DRAFT MATCHUP & BETTER HERO PICKS**
    - Evaluate draft win conditions and missing mechanics (cc, break, damage types, frontline).
    - Suggest 2–3 better alternative hero picks to counter the enemy draft and explain why.

    🛡️ **2. ESSENTIAL ITEM COUNTER BUILDS**
    - Identify specific enemy hero threats (e.g., passives, healing, evasion, ghost scepters, mobility).
    - Detail **3–4 SPEC
