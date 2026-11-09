"""Hero ability data must survive OpenDota's nested ability entries.

One hero (Monkey King) lists a nested array of second-form abilities. Iterating
it reached `name in abilities` with a list, raised "unhashable type: list", and
aborted before HERO_ABILITY_MAP was assigned, so all heroes lost ability data.
"""
from _support import fixture, game_data


def test_every_hero_has_an_ability_list():
    game = game_data()
    assert len(game.hero_abilities) == len(game.hero_names)
    assert len(game.hero_ability_details) == len(game.hero_names)


def test_the_nested_hero_recovers_its_abilities():
    game = game_data()
    assert "Boundless Strike" in game.hero_abilities["Monkey King"]
    # The nested entry holds the second-form abilities; both should appear.
    assert "Revert Form" in game.hero_abilities["Monkey King"]


def test_the_fixture_still_contains_the_nested_shape():
    # Guards the regression itself: if upstream flattens this, the test above
    # stops proving anything, so fail loudly rather than silently passing.
    entries = fixture("constants.json")["hero_abilities"]["npc_dota_hero_monkey_king"]["abilities"]
    assert any(isinstance(entry, list) for entry in entries)


def test_ability_details_carry_descriptions():
    details = game_data().hero_ability_details["Keeper of the Light"]
    assert details
    assert all("name" in d and "description" in d for d in details)


def test_a_complete_refresh_sets_the_cache_timestamp():
    # The old exception left refreshed_everything False, so the six-hour cache
    # never engaged and every poll re-fetched all four endpoints.
    assert game_data().last_refresh > 0
