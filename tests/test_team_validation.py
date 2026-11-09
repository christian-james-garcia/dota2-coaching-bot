"""Team validation must catch same-line mislabels without firing on layout.

The adjacency patterns used \\s, which matches a newline, so a roster line
ending in one team name followed by a line starting with the other team's hero
read as a mislabel. A second failed validation withholds the whole coaching
section, so a false positive silently suppressed the output.
"""
from _support import coaching_module

TEAMS = {
    "Techies": "Dire",
    "Earth Spirit": "Dire",
    "Lycan": "Radiant",
    "Keeper of the Light": "Radiant",
    "Spirit Breaker": "Radiant",
}


def _errors(text):
    return coaching_module().find_team_label_errors(text, TEAMS)


def test_correct_text_is_accepted():
    assert _errors("Lycan pressured the Dire safe lane while Techies farmed.") == []


def test_empty_inputs_are_accepted():
    assert _errors("") == []
    assert coaching_module().find_team_label_errors("anything", {}) == []


def test_parenthetical_mislabel_is_caught():
    assert _errors("Techies (Radiant) planted mines early.") != []


def test_prefix_mislabel_is_caught():
    assert _errors("Radiant Techies planted mines early.") != []


def test_possessive_mislabel_is_caught():
    assert _errors("Radiant's Techies planted mines early.") != []


def test_predicate_mislabel_is_caught():
    assert _errors("Techies is on Radiant for this draft.") != []


def test_correct_prefix_is_not_flagged():
    assert _errors("Dire Techies planted mines early.") == []


def test_a_sentence_boundary_is_not_a_mislabel():
    assert _errors("The push favoured Radiant. Techies answered with mines.") == []


def test_a_line_break_is_not_a_mislabel():
    assert _errors("Heroes seen on Radiant\nTechies held the lane.") == []


def test_a_roster_block_is_not_a_mislabel():
    panel = "- Radiant roster: Lycan, Keeper of the Light\n- Dire roster: Techies, Earth Spirit"
    assert _errors(panel) == []


def test_an_overlapping_hero_name_does_not_cross_trigger():
    # "Spirit Breaker" contains no other roster name as a whole-word match.
    assert _errors("Spirit Breaker charged in for Radiant.") == []


def test_provider_chain_skips_providers_without_a_key():
    chain = coaching_module().provider_chain
    assert [p[0] for p in chain("k", "k")] == ["GEMINI", "GROQ"]
    assert [p[0] for p in chain("k", None)] == ["GEMINI"]
    assert [p[0] for p in chain(None, "k")] == ["GROQ"]
    assert chain(None, None) == []


def test_provider_chain_carries_the_key_in_the_auth_header():
    name, url, headers, models = coaching_module().provider_chain("abc", None)[0]
    assert headers["Authorization"] == "Bearer abc"
    assert models


def test_the_correction_prompt_quotes_the_rosters_and_the_error():
    build = coaching_module().build_correction_prompt
    text = build("ORIGINAL", ["Lycan"], ["Techies"], ["Techies is Dire, not Radiant"], "DRAFT")
    assert "ORIGINAL" in text and "DRAFT" in text
    assert "Verified Radiant: Lycan" in text
    assert "Verified Dire: Techies" in text
    assert "Techies is Dire, not Radiant" in text


ABILITIES = {
    "Keeper of the Light": ["Illuminate", "Blinding Light", "Chakra Magic", "Spirit Form"],
    "Lycan": ["Summon Wolves", "Feral Impulse", "Shapeshift"],
    "Techies": ["Sticky Bomb", "Reactive Tazer", "Blast Off!"],
}
KNOWN = [a for abilities in ABILITIES.values() for a in abilities] + ["Boundless Strike", "Mischief"]


def _claim_errors(text, item_names=()):
    return coaching_module().find_ability_attribution_errors(
        text, ABILITIES, KNOWN, item_names
    )


def test_an_ability_credited_to_its_owner_is_accepted():
    assert _claim_errors("Lycan used Shapeshift to chase.") == []
    assert _claim_errors("Keeper of the Light's Chakra Magic refilled mana.") == []


def test_an_ability_credited_to_the_wrong_hero_is_caught():
    assert _claim_errors("Lycan used Chakra Magic to refill mana.") != []
    assert _claim_errors("Techies' Shapeshift closed the gap.") != []


def test_an_ability_no_hero_in_the_match_owns_is_caught():
    assert _claim_errors("Lycan opened with Boundless Strike.") != []


def test_an_ability_mentioned_without_a_hero_is_not_flagged():
    # The prompt allows discussing an ability in general terms.
    assert _claim_errors("Shapeshift is the win condition in this draft.") == []


def test_a_line_break_is_not_an_attribution():
    assert _claim_errors("Heroes: Lycan\nChakra Magic was cast early.") == []


def test_an_ability_name_that_is_also_an_item_is_skipped():
    abilities = dict(ABILITIES)
    known = KNOWN + ["Blink"]
    # "Blink" is both an ability name and an item every hero buys.
    errors = coaching_module().find_ability_attribution_errors(
        "Techies Blink Dagger timing was late.", abilities, known, item_names=["Blink"]
    )
    assert errors == []


def test_short_ability_names_are_not_checked():
    # Too collision-prone, and a false positive costs the whole section.
    errors = coaching_module().find_ability_attribution_errors(
        "Lycan Howl echoed.", {"Lycan": ["Shapeshift"]}, ["Howl"], ()
    )
    assert errors == []


def test_find_claim_errors_combines_both_checks():
    combine = coaching_module().find_claim_errors
    text = "Radiant Techies used Chakra Magic."
    errors = combine(text, TEAMS, ABILITIES, KNOWN, ())
    assert len(errors) >= 2, errors
    assert any("not Radiant" in e for e in errors)
    assert any("not Techies's ability" in e or "is not Techies" in e for e in errors)


def test_no_mentioned_ability_means_no_work():
    assert coaching_module().find_ability_attribution_errors(
        "A clean draft with no spell names.", ABILITIES, KNOWN, ()
    ) == []


def test_another_heros_action_in_the_same_sentence_is_not_a_misattribution():
    # A wildcard gap between hero and ability would read this as a claim about
    # Lycan. The verb list is what keeps it from doing so.
    text = "Lycan pressured the lane while Keeper of the Light used Chakra Magic."
    assert _claim_errors(text) == []


def test_a_possessive_on_a_hero_name_ending_in_s_is_understood():
    assert _claim_errors("Techies' Shapeshift closed the gap.") != []
    assert _claim_errors("Techies' Sticky Bomb hit three heroes.") == []
