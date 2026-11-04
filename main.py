import discord
from discord.ext import commands, tasks
from aiohttp import web
import aiohttp
import asyncio
import json
import os

# SECURE CONFIGURATION
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
LLM_API_KEY = os.environ.get("LLM_API_KEY")
CHANNEL_ID = 1567594143834702025
TRACKED_STEAM_IDS = [243689827]
OPENROUTER_MODEL = "openrouter/free"

HERO_NAME_MAP = {}
LAST_PROCESSED_MATCH = None

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Dummy web server for Render health checks
async def handle_ping(request):
    return web.Response(text="Dota 2 Coaching Bot is Online!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server started on port {port}")

async def load_hero_constants():
    global HERO_NAME_MAP
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.opendota.com/api/heroes") as resp:
            if resp.status == 200:
                heroes = await resp.json()
                HERO_NAME_MAP = {h["id"]: h["localized_name"] for h in heroes}

async def fetch_opendota_match(match_id: int):
    url = f"https://api.opendota.com/api/matches/{match_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
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
    - Detail **3–4 SPECIFIC COUNTER ITEMS** our team should have purchased, specifying EXACTLY which hero/role should have built them and against which enemy hero (e.g., "Mars (Pos 3) needed Silver Edge to Break Bristleback's passive", "Jakiro (Pos 5) needed Spirit Vessel to stop Morphling's regen", "Sven (Pos 1) needed Nullifier to purge Ghost Scepters").

    ⚔️ **3. LANING PHASE (0–10 MINS)**
    - Analyze lane matchups, CS@10, and tempo.

    🎒 **4. TIMING & POWER SPIKE AUDIT**
    - Critique core timing spikes (BKB, Blink, Manta) and highlight delays or game-losing item gaps.

    🗺️ **5. MACRO & TEAM DRILL**
    - Analyze Roshan control, map control, and give 1 operational rule for next match.

    Keep tone direct, highly tactical, and concise. Use bold text and bullet points.
    """

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://discord.com",
        "X-Title": "Dota 2 Coaching Bot"
    }
    
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": coach_prompt}]
    }

    async with aiohttp.ClientSession() as session:
        async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
            if resp.status == 200:
                result = await resp.json()
                return result["choices"][0]["message"]["content"]
            else:
                error_body = await resp.text()
                print(f"OpenRouter API Error ({resp.status}): {error_body}")
                return "⚠️ Failed to generate AI coaching debrief."

@tasks.loop(minutes=3)
async def check_recent_matches():
    global LAST_PROCESSED_MATCH
    if not TRACKED_STEAM_IDS:
        return

    steam_id = TRACKED_STEAM_IDS[0]
    url = f"https://api.opendota.com/api/players/{steam_id}/recentMatches"
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                recent = await resp.json()
                if recent:
                    latest_match_id = recent[0]["match_id"]
                    
                    if LAST_PROCESSED_MATCH is None:
                        LAST_PROCESSED_MATCH = latest_match_id
                        return

                    if latest_match_id != LAST_PROCESSED_MATCH:
                        LAST_PROCESSED_MATCH = latest_match_id
                        
                        channel = bot.get_channel(CHANNEL_ID)
                        if channel:
                            await channel.send(f"📊 **New Match Detected (Match ID: `{latest_match_id}`)!** Analyzing draft, telemetry, and item counter builds...")
                            
                            await session.post(f"https://api.opendota.com/api/request/{latest_match_id}")
                            await asyncio.sleep(12)
                            
                            match_data = await fetch_opendota_match(latest_match_id)
                            if match_data:
                                debrief_text = await generate_ai_coaching_debrief(match_data)
                                
                                if len(debrief_text) > 4000:
                                    debrief_text = debrief_text[:3900] + "\n\n*(Truncated due to Discord character limit)*"

                                embed = discord.Embed(
                                    title=f"🧠 Draft & Item Counter AI Debrief — Match {latest_match_id}",
                                    description=debrief_text,
                                    color=discord.Color.purple()
                                )
                                embed.set_footer(text="OpenDota Telemetry Engine | OpenRouter Free Tier")
                                await channel.send(embed=embed)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await load_hero_constants()
    await start_web_server()
    check_recent_matches.start()
    print("Match tracking loop started.")

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is missing!")
    bot.run(DISCORD_BOT_TOKEN)
