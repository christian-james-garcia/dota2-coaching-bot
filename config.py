"""Deployment settings and tuning constants.

Values that depend on where the bot runs come from the environment; values that
describe how it behaves are named here so a policy lives in one place rather
than at the site that enforces it.
"""
import asyncio
import json
import os
from pathlib import Path

import aiohttp

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
BUG_RETRY_LIMIT = 3

# Failures that mean the network or a remote service misbehaved, and that a
# later attempt may well survive. Anything else escaping an I/O block is a
# defect in this project: it is reported as one rather than counted as an
# outage. KeyError, TypeError and AttributeError are deliberately absent.
TRANSIENT_ERRORS = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
    json.JSONDecodeError,
)
