"""Team validation must catch same-line mislabels without firing on layout.

The adjacency patterns used \\s, which matches a newline, so a roster line
ending in one team name followed by a line starting with the other team's hero
read as a mislabel. A second failed validation withholds the whole coaching
section, so a false positive silently suppressed the output.
"""
from _support import main_module

TEAMS = {
    "Techies": "Dire",
    "Earth Spirit": "Dire",
    "Lycan": "Radiant",
    "Keeper of the Light": "Radiant",
    "Spirit Breaker": "Radiant",
}


def _errors(text):
    return main_module().find_team_label_errors(text, TEAMS)


def test_correct_text_is_accepted():
    assert _errors("Lycan pressured the Dire safe lane while Techies farmed.") == []


def test_empty_inputs_are_accepted():
    assert _errors("") == []
    assert main_module().find_team_label_errors("anything", {}) == []


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
