# Test plan

No tests exist today. This document picks the order to add them in, chosen so the early tests are
cheap to write and immediately useful as a safety net for the refactor in `docs/REFACTOR-PLAN.md`.

## Setup

```bash
pip install pytest
mkdir -p tests/fixtures
```

Add to `requirements.txt` only if you want CI to install it; otherwise keep a separate
`requirements-dev.txt` so the Render build stays slim.

## Fixtures

The OpenDota match endpoint needs no API key, so fixtures can be captured and committed with no
secret handling at all:

```bash
curl -s https://api.opendota.com/api/matches/<match_id> \
  > tests/fixtures/match_parsed.json
```

Capture at least three:

| Fixture | Why |
|---|---|
| `match_parsed.json` | A fully parsed match with `teamfights`, `purchase_log`, `benchmarks` |
| `match_unparsed.json` | A recent match before replay parsing completes — `version` null, `benchmarks` present but hollow |
| `match_no_teamfights.json` | Parsed but with an empty or missing `teamfights` array |

The second and third are the ones that matter. Most of the defensive `.get(..., default)` code in
`main.py` exists for them, and that code has never been exercised deliberately.

Constants fixtures are also keyless:

```bash
curl -s https://api.opendota.com/api/heroes      > tests/fixtures/heroes.json
curl -s https://api.opendota.com/api/constants/items > tests/fixtures/items.json
```

## Order to write them

### 1. `find_team_label_errors` — start here

17 lines, pure, no state, and it is the keystone of the honesty guarantee. If it under-detects, the
bot posts a debrief that puts a hero on the wrong team. If it over-detects, every debrief gets
withheld and the bot goes silent. Both failures are bad and neither is currently covered.

Cases:
- correct output → no errors
- a hero named on the opposing team → one error
- a hero that appears in both rosters across a long debrief → should not false-positive
- hero names that are substrings of others (Spirit Breaker vs Earth Spirit, Vengeful Spirit;
  Dragon Knight vs Knight-prefixed text) → the substring-matching behavior here is worth pinning
  down explicitly, because it is the most likely source of a silent false negative
- empty content, empty `team_by_hero`

### 2. `chunk_debrief`

32 lines, pure, and it guards a hard Discord limit. A regression here means the bot throws on send
and the match gets deferred forever.

Cases:
- text under the limit → one chunk
- text over the limit → every chunk `<= max_len`
- no chunk splits mid-word or mid-markdown-bold, since a split `**` renders as literal asterisks
- empty string → empty list, per the current early return
- a single unbreakable token longer than `max_len`

### 3. `summarize_teamfights`

150 lines of nested iteration and the most complex pure function in the file. Depends on constants,
so write this after `GameData` exists (refactor step 3) or pass the maps in by hand.

Cases:
- the parsed fixture → participation and outcome counts match a hand-audited expectation
- `teamfights` missing entirely → returns the documented empty shape, does not raise
- a fight with a player slot absent from the roster
- ability and item keys not present in the constants maps → falls back rather than `KeyError`

### 4. `extract_match_telemetry`

71 lines. Becomes pure once constants are arguments, which is the point of refactor step 3.

Cases:
- Radiant/Dire assignment from `isRadiant` for all ten players
- a tracked account id present → appears in `tracked_players`
- a tracked account id absent from the match → empty tracked list, no crash
- `account_id` null, which OpenDota returns for anonymous players
- unparsed fixture → missing `purchase_log` and `benchmarks` handled

### 5. `MatchState` backoff

After refactor step 2. The schedule is now
`min(RETRY_BACKOFF_MAX_SECONDS, RETRY_BACKOFF_BASE_SECONDS * 2**(attempts - 1))`, a single cap at
1920s (32 minutes).

Cases:
- attempt 1 → 60s, attempt 2 → 120s, attempt 6 → 1920s
- attempt 10 → still 1920s, not 3600s
- deferring a match leaves later matches in the queue eligible
- the atomic write leaves no `.tmp` file behind on success
- a corrupt state file on load → empty state, no raise
- the legacy single-cursor file migrates to the per-account shape

## Regression cases from fixes already made

Write these alongside item 1, since they are the cheapest tests in the file and both bugs shipped
unnoticed:

- **`summarize_teamfights` on a match with a full 5v5 fight returns without raising** (`c5129ff`).
  The per-hero totals dict is keyed by `(team, hero)`; a test that asserts non-empty `players` on
  the parsed fixture would have caught the mismatched lookup immediately.
- **`load_hero_constants` populates `HERO_ABILITY_MAP` for every hero** (`e253621`). The real
  assertion is `len(HERO_ABILITY_MAP) == len(HERO_NAME_MAP)`. A fixture containing Monkey King's
  nested ability list is enough; no network needed.
- **`defer_pending_match` never exceeds `RETRY_BACKOFF_MAX_SECONDS`** (`8aafab5`).
- **A transient failure never parks a match, an unexpected one parks it after `BUG_RETRY_LIMIT`**,
  a parked match is excluded from the eligibility filter while its siblings stay eligible, and
  `clear_blocked_flags` releases it with `attempts` reset. All pure, no network, no Discord.
- **`TRANSIENT_ERRORS` excludes `KeyError`, `TypeError` and `AttributeError`.** A one-line
  `issubclass` assertion, and it is the guard that keeps the two bugs above from being absorbed
  again.
- **`match_has_parsed_player_data` rejects an unparsed match** (`27d9f72`). The `match_unparsed`
  fixture is the whole test. Assert `False` on it and `True` on the parsed fixture; also cover
  `lh_ten == 0`, which is real and must not be read as missing. This is the single highest-value
  assertion in the file, because the old check passed 12 of 12 unparsed matches.
- **`chunk_debrief` never emits a chunk with an odd `**` count** (`ae69578`). Build input from
  many short bold spans so a split is forced mid-pair.
- **`find_team_label_errors` does not fire across a newline** (`65a36fa`), while still catching
  `Hero (WrongTeam)`, `WrongTeam Hero`, `WrongTeam's Hero` and `Hero is on WrongTeam` on one line.

## Not worth testing yet

- `load_hero_constants`, `fetch_opendota_match`, `get_ranked_hero_sample` — all network I/O; mock
  them only once the pure layer is covered
- `generate_ai_coaching_debrief` — the LLM response is nondeterministic. Test
  `find_team_label_errors` and the provider fallback ordering separately instead of trying to
  assert on generated text
- `check_recent_matches` — needs a Discord client; an integration test here costs more than it
  returns right now

## CI

A `.github/workflows/test.yml` running `pytest` on push is worth adding once step 1 and 2 exist.
The repo has no workflows today.
