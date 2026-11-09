# Dota 2 Coaching Bot

A Discord bot that watches your Dota 2 matches and posts an evidence-based debrief after each one.

It polls the [OpenDota API](https://docs.opendota.com/) for new matches on the accounts you track, builds a
**verified-facts panel** directly from the API response, then asks an LLM for coaching commentary on top of
those facts. Both are posted to a Discord channel as embeds.

The split matters: every number you read comes from the API, not from the model. The model is only allowed to
interpret.

## What a debrief contains

| Section | Source |
|---|---|
| Verified facts panel | Printed from the OpenDota response — scores, gold samples, per-player stats, final inventories, purchase timings |
| 🎯 Draft matchup | LLM, framed explicitly as a hypothesis |
| ⚔️ Team 5v5 review | LLM, restricted to recorded fight outcomes and teamfight contributions |
| 🛡️ Counter items | LLM, grounded in the current-patch item catalog |
| 📈 Tracked player | LLM, compared against a public ranked sample for the same hero and patch |
| 🎒 Build review | LLM, grounded in recorded final inventory |
| 🗺️ Next match drill | LLM, one measurable practice goal |

## How it guards against hallucination

This was the main design problem, and most of the code exists to solve it:

- **Facts are not generated.** `build_verified_match_facts()` renders stats from the API. The prompt forbids the
  model from restating or recalculating them.
- **Team assignments are validated.** `find_team_label_errors()` checks the model's output against the real
  rosters. On a mismatch the bot re-asks once with the error quoted back; if the second draft still misplaces a
  hero, the coaching text is withheld and only the verified panel is posted.
- **Current mechanics are supplied, not recalled.** Hero abilities and item descriptions come from OpenDota
  constants for the live patch, and the prompt marks them as authoritative.
- **Patch drift is stated.** The match patch and the constants patch are both named, so the model cannot claim
  current mechanics describe an older game.
- **Causation is fenced off.** The prompt bans claiming an item caused a win, gank, or fight unless the
  telemetry records that event.

## How it stays reliable

- **Durable queue.** New matches are discovered into a pending list persisted to disk and written atomically
  via a temp file plus `os.replace()`.
- **Per-account cursors.** Each tracked account has its own `last_processed_match`, so one stalled account
  doesn't block another.
- **Exponential backoff.** A match that fails (unparsed data, provider outage, Discord error) is deferred
  60s → 2m → 4m … capped at an hour, and later matches keep moving.
- **Delete-after-delivery.** A match leaves the queue only once every embed part is accepted by Discord.
- **Provider fallback.** Google AI Studio (`gemini-3.8-flash`, then `gemini-3.5-flash-lite`), then Groq
  (`openai/gpt-oss-120b`).
- **Chunked output.** Debriefs are split to stay under Discord's embed description limit.

## Setup

Requires Python 3.9 or newer.

```bash
git clone https://github.com/christian-james-garcia/dota2-coaching-bot.git
cd dota2-coaching-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill it in
```

Set the channel and the accounts you want tracked near the top of `main.py`:

```python
CHANNEL_ID = 1567594143834702025      # your Discord channel id
TRACKED_STEAM_IDS = [243689827]       # OpenDota 32-bit account ids
```

Your OpenDota account id is the number in your profile URL at `opendota.com/players/<id>`. Match history must
be public in Dota 2 under Settings → Options → Expose Public Match Data.

Then run it:

```bash
set -a && source .env && set +a
python main.py
```

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | Discord bot token |
| `LLM_API_KEY` | one of the two | Google AI Studio key, primary provider |
| `GROQ_API_KEY` | one of the two | Groq key, fallback provider |
| `LAST_PROCESSED_MATCH_FILE` | no | Queue location, default `last_processed_match.json` |
| `PORT` | no | Health endpoint port, default `10000` |

The bot needs the **Send Messages** and **Embed Links** permissions in the target channel. No message content
intent is required, since it defines no prefix commands.

### Deploying on Render

The bot binds an `aiohttp` health endpoint on `$PORT` returning 200 at `/`, so it runs as a Render web service.

- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`
- Set the environment variables above in the Render dashboard, never in the repo

**Known limitation:** Render's filesystem is ephemeral, so `last_processed_match.json` is lost on redeploy or
restart. The bot then re-discovers from the newest listed match, which can skip matches played during
downtime. A persistent disk or an external store fixes this — see the roadmap.

## Roadmap

- [ ] Move `CHANNEL_ID` and `TRACKED_STEAM_IDS` into configuration instead of source
- [ ] Back the queue with a persistent store so Render restarts don't lose cursors
- [ ] Split `main.py` into modules — OpenDota client, telemetry, prompting, Discord delivery
- [ ] Add tests, starting with `extract_match_telemetry`, `summarize_teamfights`, and `find_team_label_errors`
- [ ] Cache OpenDota responses to reduce calls against the public rate limit
- [ ] Slash commands for on-demand debriefs of a given match id
- [ ] Widen validation beyond team labels to item and ability claims

## Data and privacy

The bot reads public Dota 2 match data from OpenDota for the accounts listed in `TRACKED_STEAM_IDS`. A match
involves nine other players, whose public in-game statistics appear in the match response and in the verified
panel. No account names, no private data, and nothing outside public match history is collected or stored.
Only the match queue is written to disk.

## License

MIT — see [LICENSE](LICENSE).
