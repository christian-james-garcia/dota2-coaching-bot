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
