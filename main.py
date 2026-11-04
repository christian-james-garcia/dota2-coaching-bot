import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json

# CONFIGURATION
DISCORD_BOT_TOKEN = "MTU2NzU5NDU5MzAzMjIxNjcyOQ.G_qPmi.6Yrv2f1e49iHKpqzkY98_KQnocqM9DXyHBkLtI"
LLM_API_KEY = "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"
CHANNEL_ID = 1567594143834702025
TRACKED_STEAM_IDS = [243689827]
OPENROUTER_MODEL = "openrouter/free"

HERO_NAME_MAP = {}
LAST_PROCESSED_MATCH = None

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

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

async def generate_ai_coaching_debrief(match_data: dict) -> str:
    player_summaries = []
    for p in match_data.get("players", []):
        if p.get("account_id") in TRACKED_STEAM_IDS:
            hero_name = HERO_NAME_MAP.get(p.get("hero_id"), f"Hero_{p.get('hero_id')}")
            player_summaries.append({
                "hero": hero_name,
                "team": "Radiant" if p.get("isRadiant") else "Dire",
                "kda": f"{p.get('kills')}/{p.get('deaths')}/{p.get('assists')}",
                "gpm": p.get("gold_per_min"),
                "xpm": p.get("xp_per_min"),
                "hero_damage": p.get("hero_damage"),
                "tower_damage": p.get("tower_damage"),
                "last_hits": p.get("last_hits"),
                "denies": p.get("denies")
            })

    duration_mins = match_data.get("duration", 0) // 60
    winner = "Radiant" if match_data.get("radiant_win") else "Dire"

    coach_prompt = f"""
    You are an elite 7,000+ MMR Dota 2 Coach analyzing a recent 5-stack match.
    Provide a direct, high-level tactical debrief.

    Match Info:
    - Duration: {duration_mins} minutes
    - Winning Team: {winner}
    - Tracked Friends' Stats: {json.dumps(player_summaries, indent=2)}

    Format your response with the following headers (use Markdown formatting):
    1. 🎯 **DRAFT RETROSPECTIVE**
       Evaluate win conditions, lockdown, frontline, and damage output.
    2. 🎒 **ITEMIZATION & TIMING AUDIT**
       Highlight item choices, missing counter items (BKB, Vessel, Skadi, Break, MKB), or delayed power spikes.
    3. 🗺️ **MACRO & OBJECTIVES REVIEW**
       Evaluate map control, Roshan timing windows, high-ground sieges, and net worth shifts.
    4. 🧠 **ADVANCED MECHANICS LESSON**
       Teach ONE advanced Dota 2 concept relevant to this specific game (e.g., dead lane pressure, power spike grouping, creep equilibrium, itemization priority).

    Keep tone constructive, analytical, and direct. Keep length concise enough for Discord.
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
                            await channel.send(f"📊 **New Match Detected (Match ID: `{latest_match_id}`)!** Generating AI Coaching debrief...")
                            
                            await session.post(f"https://api.opendota.com/api/request/{latest_match_id}")
                            await asyncio.sleep(12)
                            
                            match_data = await fetch_opendota_match(latest_match_id)
                            if match_data:
                                debrief_text = await generate_ai_coaching_debrief(match_data)
                                
                                embed = discord.Embed(
                                    title=f"🧠 Post-Game AI Coaching Debrief — Match {latest_match_id}",
                                    description=debrief_text,
                                    color=discord.Color.purple()
                                )
                                embed.set_footer(text="OpenDota API | OpenRouter Free Model Router")
                                await channel.send(embed=embed)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await load_hero_constants()
    check_recent_matches.start()
    print("Match tracking loop started.")

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
