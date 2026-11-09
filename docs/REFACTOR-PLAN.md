# Refactor plan: splitting `main.py`

`main.py` is 1336 lines holding 25 functions, one `commands.Bot` subclass, and 14 pieces of
module-level mutable state. This document maps what actually depends on what, so the split can be
done in verifiable steps instead of one risky move.

Everything here was derived from the current `main.py` at commit `18c1112`.

## Why it needs splitting

Six distinct concerns share one namespace, and they communicate through module globals rather than
arguments. The practical cost is that nothing is testable: `extract_match_telemetry` is a pure
function on paper, but it reads `HERO_NAME_MAP`, `ITEM_NAME_MAP`, `ITEM_KEY_NAME_MAP`,
`HERO_ABILITY_MAP`, `HERO_ABILITY_DETAILS_MAP`, and `TRACKED_STEAM_IDS` off the module, so calling
it from a test means populating six globals first.

## Target layout

```
main.py                   wiring and entrypoint only
config.py                 env vars, CHANNEL_ID, TRACKED_STEAM_IDS, tuning constants
state.py                  MatchState: cursors, pending queue, backoff, atomic persistence
opendota/client.py        HTTP access, retry, response caching
opendota/gamedata.py      GameData: hero/ability/item/patch constants + refresh policy
opendota/ranked.py        get_ranked_hero_sample, average_stats, rate_stats
analysis/telemetry.py     extract_match_telemetry, summarize_teamfights
analysis/facts.py         build_verified_match_facts
coaching/prompt.py        prompt assembly, build_current_item_catalog
coaching/providers.py     provider chain and request_completion
coaching/validate.py      find_team_label_errors
delivery/discord.py       chunk_debrief, embed construction and sending
```

## The state, and who should own it

14 module-level mutable names, in three groups:

**Group 1 — OpenDota constants (9 names).** `HERO_NAME_MAP`, `HERO_UNIT_NAME_MAP`,
`HERO_ABILITY_MAP`, `HERO_ABILITY_DETAILS_MAP`, `ABILITY_NAME_MAP`, `ITEM_NAME_MAP`,
`ITEM_KEY_NAME_MAP`, `ITEM_DETAILS_MAP`, `PATCH_NAME_MAP`, plus `CURRENT_PATCH_ID`,
`CURRENT_PATCH_NAME`, `GAME_DATA_LAST_REFRESH`, `GAME_DATA_LAST_ATTEMPT`.

Written only by `load_hero_constants`. Read by `get_ranked_hero_sample`,
`extract_match_telemetry`, `summarize_teamfights`, `build_verified_match_facts`,
`build_current_item_catalog`, and `generate_ai_coaching_debrief`.

One writer, six readers, always read together → becomes a `GameData` object, constructed once in
`setup_hook` and passed down. This is the single highest-value change in the whole refactor,
because it converts five of the six readers into pure functions.

**Group 2 — match queue.** `MATCH_STATE`, `STATE_LOADED`. Mutated by `save_match_state`,
`defer_pending_match`, `discover_unseen_matches`, `check_recent_matches`, `on_ready`.
→ becomes a `MatchState` class owning the dict, the atomic write, and the backoff schedule.

**Group 3 — process singletons.** `WEB_RUNNER`, `RANKED_HERO_SAMPLE_CACHE`, `bot`.
`WEB_RUNNER` is already guarded for idempotency. The ranked cache belongs with
`get_ranked_hero_sample`.

## Hazards

These are the things that will break quietly if moved carelessly.

**1. `bot` is used 70 lines before it is defined.** `check_recent_matches` reads `bot` at lines
1246 and 1249; `bot` is assigned at line 1316. This works only because the name resolves at call
time, not definition time. Split naively and this becomes a circular import between the task loop
and the bot module. Fix: pass `bot` (or just the resolved channel) into the loop as a parameter.
Do not import it.

**2. `request_completion` closes over `session`.** It is nested inside the
`async with aiohttp.ClientSession(...)` block in `generate_ai_coaching_debrief`, not at module
level. Moving it to `coaching/providers.py` means `session` must become an explicit first argument,
and the caller must keep owning the session lifetime.

**3. `setup_hook` vs `on_ready` is load-bearing.** `setup_hook` runs once per process;
`on_ready` can fire repeatedly on Discord reconnect. Constants loading and the web server are in
`setup_hook` deliberately. The `STATE_LOADED` flag exists to stop a reconnect from reloading state
and discarding in-memory queue progress. Preserve both or reconnects start re-posting matches.

**4. `load_match_state` reads `TRACKED_STEAM_IDS`.** Not for tracking — for the legacy migration
path that converts the old single-cursor state file to the per-account format. If config moves
without this, old state files silently reset to an empty queue.

**5. Delete-after-delivery ordering.** In `check_recent_matches`, the match is removed from
`pending` and the cursor advanced *only after* every embed part is accepted by Discord. Any
reordering here trades the at-least-once guarantee for silent match loss.

**6. The bare `except Exception` in the poll loop** reads `account_id` and `match_id` via
`locals()`. If the loop body moves into a helper, that introspection stops working and the
deferral on unexpected errors silently does nothing.

## A real bug this surfaced (fixed in 2170689)

`extract_match_telemetry` and `summarize_teamfights` each run **twice per match**:

| Caller | Lines |
|---|---|
| `build_verified_match_facts` | 792, 803 |
| `generate_ai_coaching_debrief` | 1054, 1055 |

`check_recent_matches` calls both, so every debrief parses the same match twice.
`summarize_teamfights` is 150 lines of nested iteration over the teamfight log, so this is not
free. It also means a future change to extraction has two call sites that must agree.

Fixed ahead of the refactor: `build_match_analysis` derives both once in the poll loop and passes
the result to each consumer. Step 4 now only has to move the pure functions, not restructure
their callers.

## Migration order

Each step leaves the bot runnable. Commit and verify between steps.

1. **`config.py`** — move env vars and constants. No behavior change. Keep the legacy-migration
   dependency noted in hazard 4 in mind.
2. **`state.py`** — wrap `MATCH_STATE` in `MatchState`. Smallest blast radius, 5 call sites, and it
   makes the backoff schedule testable on its own.
3. **`opendota/gamedata.py`** — the `GameData` object. Largest win, largest diff. Do it alone.
4. **`analysis/telemetry.py`** — now pure, since constants arrive as arguments. Fix the double
   computation here.
5. **`analysis/facts.py`** — `build_verified_match_facts` is 246 lines and the largest function in
   the file. Worth breaking into per-section builders while moving it.
6. **`coaching/`** — prompt, providers, validation. Mind hazard 2.
7. **`delivery/discord.py`** — chunking and embeds.
8. **`main.py`** — should be wiring only by now.

## Verifying each step

No test suite exists yet, so until `docs/TEST-PLAN.md` is acted on, verification is manual:

```bash
python -c "import main"              # import graph is acyclic and nothing is undefined
python -m py_compile main.py
```

Then run against a known match id with a real `DISCORD_BOT_TOKEN` pointed at a scratch channel, and
diff the posted verified panel against the pre-refactor output. The verified panel is deterministic
given a match id, so it is a usable golden output. The LLM sections are not.

Note: step 3 changes the signature of five functions at once. That is the step most worth having a
test for first, which is the argument for doing TEST-PLAN step 1 before this one.

## A second, smaller finding (fixed in 8aafab5)

`defer_pending_match` computes its delay as:

```python
delay_seconds = min(3600, 60 * (2 ** min(attempts - 1, 5)))
```

The inner `min(attempts - 1, 5)` capped the exponent at 5, so the maximum delay was 60 × 2⁵ = 1920s
(32 minutes) and the outer `min(3600, ...)` never bound — dead code that read like a one-hour
ceiling. Now expressed as `RETRY_BACKOFF_BASE_SECONDS` and `RETRY_BACKOFF_MAX_SECONDS` with a
single cap. Behavior is unchanged at every attempt count.

## Two correctness bugs found while writing this plan

Both are fixed, and both are worth knowing about before the split because they show where this
file's size actually hurts.

**`summarize_teamfights` raised `KeyError` on every full 5v5 fight** (`c5129ff`). Totals were
stored under the tuple `(team, hero)` and read back by hero name alone. The poll loop's broad
`except Exception` swallowed it into a `[POLL ERROR]` and a deferral, so the symptom was a match
that retried forever rather than a stack trace. A 150-line function with no test is exactly where
this hides.

**Hero ability constants were silently empty** (`e253621`). OpenDota nests a list inside
`hero_abilities` for Monkey King, which made `name in abilities` raise `unhashable type: list` and
abort the loop before `HERO_ABILITY_MAP` was assigned — losing ability data for all 127 heroes
because of one. The prompt declares that data authoritative, so the guard it supports was inert.

The shared lesson: both failures were invisible because the broad handlers in
`load_hero_constants` and `check_recent_matches` converted defects into retries. That has since
been addressed directly rather than left for the split.

Each of the eleven `try` blocks now handles `TRANSIENT_ERRORS` first — `aiohttp.ClientError`,
`TimeoutError`, `OSError`, `json.JSONDecodeError` — and routes anything else through
`report_unexpected()`, which prints a `[BUG]` line plus a traceback. `KeyError`, `TypeError` and
`AttributeError` are deliberately not in that tuple, since those are the two bugs above.

The poll loop goes further: a transient failure still retries on the existing backoff forever,
while an unexpected one is parked after `BUG_RETRY_LIMIT` attempts. Retrying a deterministic
failure on a timer only buries the log line that explains it. Parked matches stay in the queue,
are skipped by the eligibility filter so they do not consume cycles, and are released on the next
restart — which for this deploy target usually means a redeploy carrying the fix.

For the refactor this means the error-handling policy is already one decision in two constants
rather than eleven scattered handlers, so moving code into modules no longer risks changing it by
accident.

## Open finding: discovery pagination is unbounded

`discover_unseen_matches` pages backwards until it reaches the saved watermark or receives a short
page. Unlike `get_ranked_hero_sample`, which stops at `RANKED_MATCH_SCAN_MAX_PAGES`, it has no page
limit. With a watermark far in the past it walks the entire match history at 100 per page with a
1.1s sleep between pages, and queues every match it finds.

This is not reachable from a cold start, because a missing watermark queues only the latest match.
It needs an old watermark, which is the state after a long outage. Left unfixed: a page cap plus a
decision about what to do with the skipped span is a design choice, not a correction.

Relevant to hazard 4 and to the Render note in the README: on this deploy target the state file is
ephemeral, so the usual outcome is a *missing* watermark rather than an old one, which takes the
safe path instead.
