"""Rebuilding the queue cursor from the channel.

The queue is written to disk atomically, but this project's host has an
ephemeral filesystem, so that file is gone after every redeploy. Discovery then
re-anchors to the newest listed match and silently skips whatever was played
during the downtime. What was actually posted is recorded in the channel, so
that is read back instead.
"""
import asyncio
import pathlib
import tempfile

from _support import delivery_module, main_module, state_module


class _Author:
    def __init__(self, id):
        self.id = id


class _Message:
    def __init__(self, author_id, embeds):
        self.author = _Author(author_id)
        self.embeds = embeds


class _Channel:
    def __init__(self, messages, raises=None):
        self.messages = messages
        self.raises = raises

    def history(self, limit=None):
        messages, raises = self.messages[:limit], self.raises

        class _Iter:
            def __aiter__(self):
                self._i = 0
                return self

            async def __anext__(self):
                if raises is not None:
                    raise raises
                if self._i >= len(messages):
                    raise StopAsyncIteration
                message = messages[self._i]
                self._i += 1
                return message

        return _Iter()


def _embed(match_id, account_id=None, part="Part 1 of 1"):
    delivery = delivery_module()
    discord = main_module().discord
    embed = discord.Embed(title=f"Dota 2 AI Debrief — Match {match_id}", description="x")
    footer = f"OpenDota Telemetry Engine | {part}"
    if account_id is not None:
        footer += f" | acct {account_id}"
    embed.set_footer(text=footer)
    return embed


def _run(coro):
    return asyncio.run(coro)


BOT_ID = 777


def test_reads_back_the_highest_match_per_account():
    delivery = delivery_module()
    channel = _Channel([
        _Message(BOT_ID, [_embed(100, 1)]),
        _Message(BOT_ID, [_embed(300, 1)]),
        _Message(BOT_ID, [_embed(200, 2)]),
    ])
    assert _run(delivery.recover_cursors(channel, BOT_ID, [1, 2])) == {1: 300, 2: 200}


def test_ignores_messages_from_other_authors():
    delivery = delivery_module()
    channel = _Channel([
        _Message(BOT_ID, [_embed(100, 1)]),
        _Message(999, [_embed(500, 1)]),
    ])
    assert _run(delivery.recover_cursors(channel, BOT_ID, [1])) == {1: 100}


def test_ignores_accounts_that_are_not_tracked():
    delivery = delivery_module()
    channel = _Channel([_Message(BOT_ID, [_embed(100, 5)])])
    assert _run(delivery.recover_cursors(channel, BOT_ID, [1])) == {}


def test_an_embed_without_an_account_is_attributed_to_the_sole_account():
    # Debriefs posted before the footer carried an account id.
    delivery = delivery_module()
    channel = _Channel([_Message(BOT_ID, [_embed(100)])])
    assert _run(delivery.recover_cursors(channel, BOT_ID, [7])) == {7: 100}


def test_an_embed_without_an_account_is_ignored_when_several_are_tracked():
    # Guessing which account an old post belonged to could skip matches.
    delivery = delivery_module()
    channel = _Channel([_Message(BOT_ID, [_embed(100)])])
    assert _run(delivery.recover_cursors(channel, BOT_ID, [7, 8])) == {}


def test_an_unreadable_channel_recovers_nothing_rather_than_raising():
    delivery = delivery_module()
    discord = main_module().discord
    channel = _Channel([], raises=discord.DiscordException("no access"))
    assert _run(delivery.recover_cursors(channel, BOT_ID, [1])) == {}


def test_a_multipart_debrief_is_read_once():
    delivery = delivery_module()
    channel = _Channel([
        _Message(BOT_ID, [_embed(100, 1, "Part 1 of 3"), _embed(100, 1, "Part 2 of 3")]),
    ])
    assert _run(delivery.recover_cursors(channel, BOT_ID, [1])) == {1: 100}


def test_read_delivery_tolerates_an_unrelated_embed():
    delivery = delivery_module()
    discord = main_module().discord
    embed = discord.Embed(title="Something else", description="x")
    assert delivery.read_delivery(embed) == (None, None)


def _queue(pending=None, accounts=None):
    MatchState = state_module().MatchState
    path = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    return MatchState(path, accounts=accounts, pending=pending or [])


def test_accounts_without_a_cursor_lists_only_the_gaps():
    queue = _queue(accounts={"1": {"last_processed_match": 100}, "2": {"last_processed_match": None}})
    assert queue.accounts_without_a_cursor([1, 2, 3]) == [2, 3]


def test_adopting_cursors_fills_gaps_and_leaves_stored_values_alone():
    queue = _queue(accounts={"1": {"last_processed_match": 100}})
    queue.adopt_cursors({1: 999, 2: 500})
    assert queue.accounts["1"]["last_processed_match"] == 100, "stored cursor is authoritative"
    assert queue.accounts["2"]["last_processed_match"] == 500
    assert queue.accounts["2"]["discovered_through_match"] == 500


def test_adopting_nothing_writes_nothing():
    queue = _queue()
    queue.adopt_cursors({})
    assert not queue.path.exists()


def test_send_debrief_records_the_account_for_later_recovery():
    delivery = delivery_module()

    class _Collect:
        def __init__(self):
            self.sent = []

        async def send(self, embed=None):
            self.sent.append(embed)

    channel = _Collect()
    _run(delivery.send_debrief(channel, 4242, "a short debrief", 99))
    assert delivery.read_delivery(channel.sent[0]) == (4242, 99)
