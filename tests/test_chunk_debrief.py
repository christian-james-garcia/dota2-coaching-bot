"""Chunks must stay under Discord's limit without orphaning a bold marker.

A split between ** and its partner renders as literal asterisks, and the prompt
asks for bold on exactly the takeaways meant to stand out.
"""
from _support import main_module

LIMIT = 3800


def test_empty_text_produces_no_chunks():
    assert main_module().chunk_debrief("") == []


def test_short_text_is_one_chunk():
    assert main_module().chunk_debrief("a short debrief") == ["a short debrief"]


def test_every_chunk_respects_the_limit():
    body = "\n\n".join(f"paragraph {i} " + "x" * 600 for i in range(40))
    chunks = main_module().chunk_debrief(body)
    assert chunks
    assert all(len(chunk) <= LIMIT for chunk in chunks)


def test_an_unbreakable_run_is_split_and_preserved():
    body = "y" * 9000
    chunks = main_module().chunk_debrief(body)
    assert all(len(chunk) <= LIMIT for chunk in chunks)
    assert "".join(chunks) == body


def test_no_chunk_orphans_a_bold_marker():
    body = "intro\n\n" + " ".join(f"**stat {i} is high**" for i in range(400))
    chunks = main_module().chunk_debrief(body)
    assert all(chunk.count("**") % 2 == 0 for chunk in chunks)


def test_an_unbalanced_draft_still_terminates():
    # A stray marker cannot be balanced; chunking must not loop or drop text.
    body = "**opening " + "z" * 8000
    chunks = main_module().chunk_debrief(body)
    assert all(len(chunk) <= LIMIT for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == body.replace(" ", "")


def test_balanced_split_point_falls_back_when_nothing_balances():
    bot = main_module()
    text = "**" + "a" * 100
    assert bot.balanced_split_point(text, 50) == 50
