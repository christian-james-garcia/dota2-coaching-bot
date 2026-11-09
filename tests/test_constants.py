"""Hero ability data must survive OpenDota's nested ability entries.

One hero (Monkey King) lists a nested array of second-form abilities. Iterating
it reached `name in abilities` with a list, raised "unhashable type: list", and
aborted before HERO_ABILITY_MAP was assigned, so all heroes lost ability data.
"""
from _support import fixture, with_constants


def test_every_hero_has_an_ability_list():
    bot = with_constants()
    assert len(bot.HERO_ABILITY_MAP) == len(bot.HERO_NAME_MAP)
    assert len(bot.HERO_ABILITY_DETAILS_MAP) == len(bot.HERO_NAME_MAP)


def test_the_nested_hero_recovers_its_abilities():
    bot = with_constants()
    assert "Boundless Strike" in bot.HERO_ABILITY_MAP["Monkey King"]
    # The nested entry holds the second-form abilities; both should appear.
    assert "Revert Form" in bot.HERO_ABILITY_MAP["Monkey King"]


def test_the_fixture_still_contains_the_nested_shape():
    # Guards the regression itself: if upstream flattens this, the test above
    # stops proving anything, so fail loudly rather than silently passing.
    entries = fixture("constants.json")["hero_abilities"]["npc_dota_hero_monkey_king"]["abilities"]
    assert any(isinstance(entry, list) for entry in entries)


def test_ability_details_carry_descriptions():
    bot = with_constants()
    details = bot.HERO_ABILITY_DETAILS_MAP["Keeper of the Light"]
    assert details
    assert all("name" in d and "description" in d for d in details)


def test_a_complete_refresh_sets_the_cache_timestamp():
    # The old exception left refreshed_everything False, so the six-hour cache
    # never engaged and every poll re-fetched all four endpoints.
    bot = with_constants()
    assert bot.GAME_DATA_LAST_REFRESH > 0
