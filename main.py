"""Entry point: wiring, and the loop that drives one match to delivery.

Everything substantive lives in the modules imported below. What remains here
is the order of operations for a single queued match, which is the one thing
worth reading in one place.
"""
import aiohttp
import discord
from discord.ext import commands, tasks

from analysis import build_match_analysis
from coaching import generate_ai_coaching_debrief
from config import (
    CHANNEL_ID,
    DISCORD_BOT_TOKEN,
    GROQ_API_KEY,
    LLM_API_KEY,
    STATE_FILE,
    TRACKED_STEAM_IDS,
    TRANSIENT_ERRORS,
)
from delivery import send_debrief
from facts import build_verified_match_facts
from gamedata import GameData
from health import start_web_server
from opendota import add_ranked_context, discover_unseen_matches, fetch_parsed_match
from reporting import report_unexpected
from state import MatchState

intents = discord.Intents.default()
# Message content intent is not needed because this bot defines no prefix commands.


class CoachingBot(commands.Bot):
    """Owns the queue, the game data, and the polling loop.

    These were module globals, and the loop reached for the bot by name before
    it was assigned, which worked only because the name resolved at call time.
    As instance state there is no forward reference and no import cycle to
    create when the loop is read or moved.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = MatchState(STATE_FILE)
        self.game = GameData()
        self.state_loaded = False

    async def setup_hook(self):
        # setup_hook runs once per bot process, unlike on_ready which can recur.
        await self.game.refresh()
        try:
            await start_web_server()
        except OSError as exc:
            print(f"[SERVER] Could not start health endpoint: {exc}", flush=True)

    async def on_ready(self):
        print(f"[BOT] Logged in as {self.user} (ID: {self.user.id})", flush=True)
        if not self.state_loaded:
            # A restart is usually a redeploy, so parked matches get another try.
            self.state = MatchState.load(STATE_FILE, TRACKED_STEAM_IDS).release_blocked()
            self.state_loaded = True
        if not self.poll_matches.is_running():
            self.poll_matches.start()
            print("[BOT] Match tracking loop started.", flush=True)

    async def resolve_channel(self):
        """The target channel, or None if Discord will not hand it over."""
        channel = self.get_channel(CHANNEL_ID)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(CHANNEL_ID)
        except discord.DiscordException as exc:
            print(f"[DISCORD] Could not access channel {CHANNEL_ID}: {exc}", flush=True)
            return None

    def next_match(self):
        """The oldest match whose backoff has elapsed. Match IDs are chronological."""
        eligible = self.state.eligible()
        if not eligible:
            return None, None
        pending = min(eligible, key=lambda item: int(item["match_id"]))
        return int(pending["account_id"]), int(pending["match_id"])

    async def deliver(self, session, channel, account_id, match_id):
        """Take one queued match all the way to Discord, or defer it."""
        match_data = await fetch_parsed_match(session, match_id)
        if not match_data:
            print(f"[RETRY] Match {match_id} is not ready; will retry next cycle.", flush=True)
            self.state.defer(account_id, match_id, "OpenDota match data unavailable")
            return

        await add_ranked_context(session, match_data, self.game, TRACKED_STEAM_IDS)
        analysis = build_match_analysis(match_data, self.game, TRACKED_STEAM_IDS)

        debrief_text = await generate_ai_coaching_debrief(
            match_data, analysis, self.game, LLM_API_KEY, GROQ_API_KEY
        )
        if not debrief_text:
            print(f"[RETRY] AI providers failed for match {match_id}; will retry next cycle.", flush=True)
            self.state.defer(account_id, match_id, "AI providers unavailable")
            return

        verified_facts = build_verified_match_facts(match_data, analysis, self.game)
        await send_debrief(channel, match_id, f"{verified_facts}\n\n---\n\n{debrief_text}")

        # Only now: a failure above leaves the match queued for a full re-send.
        self.state.complete(account_id, match_id)
        print(f"[QUEUE] Successfully delivered match {match_id}.", flush=True)

    @tasks.loop(minutes=3)
    async def poll_matches(self):
        if not TRACKED_STEAM_IDS:
            return
        await self.game.refresh()

        # Named before the try so the handlers can defer without inspecting
        # locals(), which silently stops working if this body is ever moved.
        account_id = match_id = None
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for tracked_id in TRACKED_STEAM_IDS:
                    await discover_unseen_matches(session, tracked_id, self.state)

                account_id, match_id = self.next_match()
                if match_id is None:
                    return
                print(f"[QUEUE] Processing match {match_id} for account {account_id}.", flush=True)

                channel = await self.resolve_channel()
                if channel is None:
                    self.state.defer(account_id, match_id, "Discord channel unavailable")
                    return

                await self.deliver(session, channel, account_id, match_id)
        except (discord.DiscordException,) + TRANSIENT_ERRORS as exc:
            print(f"[POLL] Transient failure during match polling: {exc}", flush=True)
            if match_id is not None:
                self.state.defer(account_id, match_id, "a transient service failure")
        except Exception as exc:
            report_unexpected("processing a queued match", exc)
            if match_id is not None:
                self.state.defer(
                    account_id, match_id, "an unexpected processing error", fatal=True
                )


bot = CoachingBot(command_prefix="!", intents=intents)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is missing!")
    if not LLM_API_KEY and not GROQ_API_KEY:
        print("[CONFIG] Neither LLM_API_KEY nor GROQ_API_KEY is set; AI reports will retry without posting.", flush=True)
    bot.run(DISCORD_BOT_TOKEN)
