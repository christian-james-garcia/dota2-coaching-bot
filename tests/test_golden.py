"""Frozen output for a real parsed match.

This is the net under the module split in docs/REFACTOR-PLAN.md. Name-based
tests move with the code being moved, so they cannot prove a refactor changed
nothing. These expectations were captured before the split and must not change
during it: if a step alters the rendered panel or the derived analysis by one
byte, it was not a refactor.

Regenerate deliberately, never to make a failure go away:
    python3 tests/test_golden.py --regenerate
"""
import json
import os
import sys

from _support import FIXTURES, analysis_module, fixture, game_data, main_module, \
    match_without_full_five_v_fives, match_without_teamfights

GOLDEN = os.path.join(FIXTURES, "golden_output.json")


def _actual():
    bot = main_module()
    module = analysis_module()
    game = game_data()
    expected = json.load(open(GOLDEN, encoding="utf-8"))
    match = fixture("match_parsed.json")
    tracked = expected["tracked_steam_ids"]
    analysis = module.build_match_analysis(match, game, tracked)
    panel = bot.build_verified_match_facts(match, analysis, game)
    return expected, {
        "tracked_steam_ids": tracked,
        "analysis": analysis,
        "panel": panel,
        "panel_chunks": bot.chunk_debrief(panel),
        "item_catalog_size": len(game.item_catalog()),
        "no_full_5v5": module.summarize_teamfights(match_without_full_five_v_fives(), game, []),
        "empty_teamfights": module.summarize_teamfights(match_without_teamfights(), game, []),
    }


def test_derived_analysis_is_unchanged():
    expected, actual = _actual()
    # Compare through JSON so tuple/list differences surface rather than hide.
    assert json.loads(json.dumps(actual["analysis"], sort_keys=True)) == \
        json.loads(json.dumps(expected["analysis"], sort_keys=True))


def test_rendered_panel_is_byte_identical():
    expected, actual = _actual()
    assert actual["panel"] == expected["panel"]


def test_panel_chunking_is_unchanged():
    expected, actual = _actual()
    assert actual["panel_chunks"] == expected["panel_chunks"]


def test_teamfight_edge_cases_are_unchanged():
    expected, actual = _actual()
    assert json.loads(json.dumps(actual["no_full_5v5"], sort_keys=True)) == \
        json.loads(json.dumps(expected["no_full_5v5"], sort_keys=True))
    assert actual["empty_teamfights"] == expected["empty_teamfights"]


def test_item_catalog_size_is_unchanged():
    expected, actual = _actual()
    assert actual["item_catalog_size"] == expected["item_catalog_size"]


if __name__ == "__main__":
    if "--regenerate" not in sys.argv:
        print(__doc__)
        raise SystemExit(2)
    _, actual = _actual()
    json.dump(actual, open(GOLDEN, "w"), indent=1, sort_keys=True)
    print(f"regenerated {GOLDEN}")
