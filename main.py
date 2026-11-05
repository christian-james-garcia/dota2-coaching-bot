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

# Dynamic Free Auto-Router
OPENROUTER_MODEL = "cohere/north-mini-code:free"

HERO_NAME_MAP = {}
LAST_PROCESSED_MATCH = 0

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
    print(f"[SERVER] Web server successfully bound to port {port}", flush=True)

async def load_hero_constants():
    global HERO_NAME_MAP
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("https://api.opendota.com/api/heroes") as resp:
                if resp.status == 200:
                    heroes = await resp.json()
                    HERO_NAME_MAP = {h["id"]: h["localized_name"] for h in heroes}
                    print(f"[HEROES] Loaded {len(HERO_NAME_MAP)} hero constants.", flush=True)
                else:
                    print(f"[HEROES] Failed to fetch hero constants. Status: {resp.status}", flush=True)
        except Exception as e:
            print(f"[HEROES] Exception loading hero constants: {e}", flush=True)

async def fetch_opendota_match(match_id: int):
    url = f"https://api.opendota.com/api/matches/{match_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                print(f"[OPENDOTA] Match {match_id} fetch failed with status {resp.status}", flush=True)
        except Exception as e:
            print(f"[OPENDOTA] Error fetching match {match_id}: {e}", flush=True)
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
    
    radiant_str = ", ".join(radiant_lineup)
    dire_str = ", ".join(dire_lineup)
    tracked_json = json.dumps(tracked_players, indent=2)

    coach_prompt = (
        f"You are an elite 7,000+ MMR Dota 2 Coach analyzing a competitive team match.\n"
        f"Accurately analyze the draft, item builds, and laning phase using exact Dota 2 mechanics.\n"
        f"Do NOT invent non-existent spells or confuse hero skill sets.\n\n"
        f"MATCH METRICS:\n"
        f"- Duration: {duration_mins} mins | Winner: {winner}\n"
        f"- Radiant Draft: {radiant_str}\n"
        f"- Dire Draft: {dire_str}\n"
        f"- Our Tracked Player Performance: {tracked_json}\n\n"
        f"Provide concise, high-MMR analysis under these headers:\n\n"
        f"🎯 **1. DRAFT MATCHUP & BETTER PICKS**\n"
        f"- Evaluate draft win conditions and missing mechanics (lockdown, break, dispel, physical/magic split).\n"
        f"- Suggest 2 situational alternative hero picks to solve team composition issues.\n\n"
        f"🛡️ **2. ITEM COUNTER BUILDS**\n"
        f"- Identify top 3 enemy hero threats.\n"
        f"- Detail **3 SPECIFIC COUNTER ITEMS** our team missed or delayed, specifying EXACTLY which hero should build them.\n\n"
        f"⚔️ **3. LANING & CS AT 10 MINS**\n"
        f"- Critique laning performance, CS@10, and wave control.\n\n"
        f"🎒 **4. TIMING & POWER SPIKES**\n"
        f"- Audit key item timings (BKB, Blink, Manta) and point out key delays.\n\n"
        f"🗺️ **5. MACRO & NEXT GAME DRILL**\n"
        f"- Provide 1 concrete team macro rule for next game.\n\n"
        f"CRITICAL REQUIREMENT: Keep total output under 2,800 characters to fit Discord limits. Be direct and concise."
    )

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://discord.com",
        "X-Title": "Dota 2 Coaching Bot"
    }
    
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": coach_prompt}],
        "max_tokens": 1000
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    choices = result.get("choices", [])
                    if choices and len(choices) > 0:
                        return choices[0]["message"]["content"]
                    return "⚠️ AI returned an empty response."
                else:
                    error_body = await resp.text()
                    print(f"[OPENROUTER] API Error ({resp.status}): {error_body}", flush=True)
                    return f"⚠️ Failed to generate AI coaching debrief (API Status: {resp.status})."
        except Exception as e:
            print(f"[OPENROUTER] Exception requesting completion: {e}", flush=True)
            return "⚠️ AI Service connectivity failure."

@tasks.loop(minutes=3)
async def check_recent_matches():
    global LAST_PROCESSED_MATCH
    if not TRACKED_STEAM_IDS:
        return

    steam_id = TRACKED_STEAM_IDS[0]
    url = f"https://api.opendota.com/api/players/{steam_id}/recentMatches"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    recent = await resp.json()
                    if recent:
                        latest_match_id = recent[0]["match_id"]
                        
                        if LAST_PROCESSED_MATCH is None:
                            LAST_PROCESSED_MATCH = latest_match_id
                            print(f"[POLL] Initialized baseline match ID: {latest_match_id}. Polling active...", flush=True)
                            return

                        print(f"[POLL] Polling check: Latest={latest_match_id} | Cached={LAST_PROCESSED_MATCH}", flush=True)
                        
                        if latest_match_id != LAST_PROCESSED_MATCH:
                            LAST_PROCESSED_MATCH = latest_match_id
                            print(f"[MATCH] New match detected! ID: {latest_match_id}", flush=True)
                            
                            channel = bot.get_channel(CHANNEL_ID)
                            if channel:
                                await channel.send(f"📊 **New Match Detected (Match ID: `{latest_match_id}`)!** Analyzing draft, telemetry, and item counter builds...")
                                
                                parse_req = await session.post(f"https://api.opendota.com/api/request/{latest_match_id}")
                                print(f"[OPENDOTA] Parse request status: {parse_req.status}", flush=True)
                                await asyncio.sleep(12)
                                
                                match_data = await fetch_opendota_match(latest_match_id)
                                if match_data:
                                    debrief_text = await generate_ai_coaching_debrief(match_data)
                                    
                                    # Fallback guard against NoneType
                                    if not debrief_text:
                                        debrief_text = "⚠️ Unable to generate debrief text from OpenRouter."
                                    
                                    if len(debrief_text) > 4000:
                                        debrief_text = debrief_text[:3900] + "\n\n*(Truncated due to Discord character limit)*"

                                    embed = discord.Embed(
                                        title=f"🧠 Draft & Item Counter AI Debrief — Match {latest_match_id}",
                                        description=debrief_text,
                                        color=discord.Color.purple()
                                    )
                                    embed.set_footer(text="OpenDota Telemetry Engine | OpenRouter Free Router")
                                    await channel.send(embed=embed)
                            else:
                                print(f"[DISCORD] Could not access channel ID: {CHANNEL_ID}", flush=True)
                elif resp.status == 429:
                    print("[POLL] OpenDota API rate limited (429). Retrying next cycle.", flush=True)
                else:
                    print(f"[POLL] OpenDota API return code: {resp.status}", flush=True)
    except Exception as e:
        print(f"[POLL ERROR] Exception during match polling: {e}", flush=True)
        traceback.print_exc()

@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    await load_hero_constants()
    await start_web_server()
    if not check_recent_matches.is_running():
        check_recent_matches.start()
        print("[BOT] Match tracking loop started.", flush=True)

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is missing!")
    bot.run(DISCORD_BOT_TOKEN)
