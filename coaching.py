"""Asking a model for coaching, and refusing to publish it when it is wrong.

The prompt is assembled here and nothing in it is improvised: rosters, current
ability text and the item catalog are supplied so the model never has to recall
them, and the sections it is asked for forbid claiming an unobserved cause.

request_completion takes its session as an argument. It used to be nested
inside the `async with` block that opened one, closing over it, which meant the
provider chain could not be read or tested apart from the request that drove it.
"""
import json
import re
from typing import Optional

import aiohttp

from gamedata import GameData
from reporting import report_unexpected
from config import TRANSIENT_ERRORS

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash-lite")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = ("openai/gpt-oss-120b",)
MAX_COMPLETION_TOKENS = 2500
REQUEST_TIMEOUT_SECONDS = 45
WITHHELD_NOTICE = (
    "\u26a0\ufe0f AI coaching text withheld because team assignments failed "
    "validation. See the verified facts above."
)


def provider_chain(llm_api_key: Optional[str], groq_api_key: Optional[str]) -> list:
    """Providers to try in order, skipping any whose key is absent."""
    chain = []
    if llm_api_key:
        chain.append((
            "GEMINI",
            GEMINI_URL,
            {"Authorization": f"Bearer {llm_api_key}", "Content-Type": "application/json"},
            GEMINI_MODELS,
        ))
    if groq_api_key:
        chain.append((
            "GROQ",
            GROQ_URL,
            {"Authorization": f"Bearer {groq_api_key}", "Content-Type": "application/json"},
            GROQ_MODELS,
        ))
    return chain


def find_team_label_errors(content: str, team_by_hero: dict) -> list:
    """Detect explicit hero/team labels that contradict OpenDota's player slots."""
    errors = []
    for hero, actual_team in team_by_hero.items():
        wrong_team = "Dire" if actual_team == "Radiant" else "Radiant"
        escaped_hero = re.escape(hero)
        # The two adjacency patterns deliberately match spaces and tabs but not
        # newlines. With \s they fired across a line break, so a roster list
        # ending in "Radiant" followed by a line starting with a Dire hero was
        # read as a mislabel. That cost the whole coaching section, since a
        # second failed validation withholds it. A mislabel split over two lines
        # now goes undetected, which is the cheaper error: the verified panel
        # directly above always prints the true rosters.
        patterns = (
            rf"\b{escaped_hero}\s*\(\s*{wrong_team}\s*\)",
            rf"\b{wrong_team}[ \t]+{escaped_hero}\b",
            rf"\b{wrong_team}[’']s[ \t]+{escaped_hero}\b",
            rf"\b{escaped_hero}\s+(?:is|was|plays|played)\s+(?:on|for|with)\s+{wrong_team}\b",
        )
        if any(re.search(pattern, content, flags=re.IGNORECASE) for pattern in patterns):
            errors.append(f"{hero} is {actual_team}, not {wrong_team}")
    return errors


def build_coach_prompt(match_data: dict, analysis: dict, game: GameData) -> str:
    """Assemble the coaching prompt from verified match data."""
    radiant_lineup = analysis["radiant"]
    dire_lineup = analysis["dire"]
    tracked_players = analysis["tracked_players"]
    all_players = analysis["all_players"]
    teamfight_summary = analysis["teamfights"]
    duration_mins = match_data.get("duration", 0) // 60
    winner = "Radiant" if match_data.get("radiant_win") else "Dire"
    current_item_catalog = game.item_catalog()
    match_patch_id = match_data.get("patch")
    match_patch_name = (
        game.patch_label(match_patch_id)
    )

    coach_prompt = (
        "You are a concise, evidence-led Dota 2 coach. Analyze the actual match data below. "
        "Use hero mechanics only when you are confident they are current. Never invent spell usage, "
        "lane events, or purchases. Say plainly when a field is unavailable.\n\n"
        "FORMATTING: The verified panel starts with concise match highlights. In your coaching sections, use Discord Markdown bold selectively for the most important team insight, the tracked player's key comparison, the recommended improvement, and the practice drill. Do not bold whole paragraphs or every statistic.\n"
        f"The match was played on patch {match_patch_name}. Your current-mechanics reference is OpenDota's "
        f"latest constants for patch {game.current_patch_name or 'unavailable'}; use those current abilities and the "
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
    return coach_prompt


def build_correction_prompt(coach_prompt: str, radiant_lineup, dire_lineup,
                            errors: list, draft: str) -> str:
    """Re-ask once, quoting the rosters and the specific error back."""
    return (
        f"{coach_prompt}\n\n"
        "Your previous draft assigned a hero to the wrong team. Rewrite the complete response "
        "using the verified rosters below, and make every matchup cross-team.\n"
        f"Verified Radiant: {', '.join(radiant_lineup)}\n"
        f"Verified Dire: {', '.join(dire_lineup)}\n"
        f"Detected error(s): {'; '.join(errors)}\n"
        f"Previous draft:\n{draft}"
    )


async def request_completion(session: aiohttp.ClientSession, provider_name: str, url: str,
                             headers: dict, model_slug: str, prompt: str) -> Optional[str]:
    """One completion attempt. Returns None on any failure, having logged it."""
    payload = {
        "model": model_slug,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_COMPLETION_TOKENS,
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
    except TRANSIENT_ERRORS as exc:
        print(f"[{provider_name}] Request failed for {model_slug}: {exc}", flush=True)
    except Exception as exc:
        report_unexpected(f"{provider_name} request with {model_slug}", exc)
    return None


async def generate_ai_coaching_debrief(match_data: dict, analysis: dict, game: GameData,
                                       llm_api_key: Optional[str],
                                       groq_api_key: Optional[str]) -> Optional[str]:
    """Coaching text for a match, or None, or a notice that it was withheld."""
    radiant_lineup = analysis["radiant"]
    dire_lineup = analysis["dire"]
    team_by_hero = {player["hero"]: player["team"] for player in analysis["all_players"]}
    coach_prompt = build_coach_prompt(match_data, analysis, game)
    providers = provider_chain(llm_api_key, groq_api_key)
    team_validation_failed = False

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for provider_name, url, headers, models in providers:
            for model_slug in models:
                content = await request_completion(
                    session, provider_name, url, headers, model_slug, coach_prompt
                )
                if not content:
                    continue

                errors = find_team_label_errors(content, team_by_hero)
                if errors:
                    corrected = await request_completion(
                        session, provider_name, url, headers, model_slug,
                        build_correction_prompt(coach_prompt, radiant_lineup,
                                                dire_lineup, errors, content),
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

    if not providers:
        print("[AI] No LLM_API_KEY or GROQ_API_KEY is configured.", flush=True)
    if team_validation_failed:
        return WITHHELD_NOTICE
    return None
