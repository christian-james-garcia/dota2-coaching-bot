"""Chunks must stay under Discord's limit without orphaning a bold marker.

A split between ** and its partner renders as literal asterisks, and the prompt
asks for bold on exactly the takeaways meant to stand out.
"""
from _support import delivery_module

LIMIT = 3800


def test_empty_text_produces_no_chunks():
    assert delivery_module().chunk_debrief("") == []


def test_short_text_is_one_chunk():
    assert delivery_module().chunk_debrief("a short debrief") == ["a short debrief"]


def test_every_chunk_respects_the_limit():
    body = "\n\n".join(f"paragraph {i} " + "x" * 600 for i in range(40))
    chunks = delivery_module().chunk_debrief(body)
    assert chunks
    assert all(len(chunk) <= LIMIT for chunk in chunks)


def test_an_unbreakable_run_is_split_and_preserved():
    body = "y" * 9000
    chunks = delivery_module().chunk_debrief(body)
    assert all(len(chunk) <= LIMIT for chunk in chunks)
    assert "".join(chunks) == body


def test_no_chunk_orphans_a_bold_marker():
    body = "intro\n\n" + " ".join(f"**stat {i} is high**" for i in range(400))
    chunks = delivery_module().chunk_debrief(body)
    assert all(chunk.count("**") % 2 == 0 for chunk in chunks)


def test_an_unbalanced_draft_still_terminates():
    # A stray marker cannot be balanced; chunking must not loop or drop text.
    body = "**opening " + "z" * 8000
    chunks = delivery_module().chunk_debrief(body)
    assert all(len(chunk) <= LIMIT for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == body.replace(" ", "")


def test_balanced_split_point_falls_back_when_nothing_balances():
    bot = delivery_module()
    text = "**" + "a" * 100
    assert bot.balanced_split_point(text, 50) == 50


class _FakeChannel:
    """Records what send_debrief would post."""

    def __init__(self, fail_on=None):
        self.sent = []
        self.fail_on = fail_on

    async def send(self, embed=None):
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise RuntimeError("Discord rejected the embed")
        self.sent.append(embed)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_send_debrief_sends_one_embed_per_chunk():
    delivery = delivery_module()
    text = "\n\n".join(f"paragraph {i} " + "x" * 600 for i in range(40))
    expected = len(delivery.chunk_debrief(text))
    channel = _FakeChannel()
    assert _run(delivery.send_debrief(channel, 123, text)) == expected
    assert len(channel.sent) == expected
    assert expected > 1, "this fixture should need splitting"
    assert f"(Part 1/{expected})" in channel.sent[0].title
    assert f"(Part {expected}/{expected})" in channel.sent[-1].title


def test_a_short_debrief_is_a_single_embed():
    delivery = delivery_module()
    channel = _FakeChannel()
    assert _run(delivery.send_debrief(channel, 123, "short debrief text")) == 1
    assert len(channel.sent) == 1
    only = channel.sent[0]
    # A single-part debrief should not be labelled "Part 1/1".
    assert "Part" not in only.title
    assert only.description == "short debrief text"
    assert only.footer.text == "OpenDota Telemetry Engine | Part 1 of 1"


def test_a_failure_part_way_through_propagates():
    # The caller advances the queue only on a clean return, so a mid-send
    # failure must not be swallowed here.
    delivery = delivery_module()
    text = "\n\n".join(f"paragraph {i} " + "x" * 600 for i in range(40))
    channel = _FakeChannel(fail_on=1)
    try:
        _run(delivery.send_debrief(channel, 123, text))
    except RuntimeError:
        pass
    else:
        raise AssertionError("send_debrief swallowed a send failure")
