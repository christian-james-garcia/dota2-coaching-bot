"""The on-demand debrief command.

The pipeline is shared with the polling loop so there is one ordering of the
steps, and the command deliberately leaves the queue alone: asking for an old
match should not move a cursor, and asking for a queued one should not mark it
delivered.
"""
import asyncio

from _support import main_module


def _run(coro):
    return asyncio.run(coro)


class _Response:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, content, ephemeral=False):
        self.messages.append(content)

    async def defer(self, ephemeral=False, thinking=False):
        self.deferred = True


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append(content)


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.followup = _Followup()


def test_the_command_is_registered_on_the_tree():
    bot = main_module().bot
    names = [options.get("name") for options, _ in bot.tree.registered]
    assert "debrief" in names


def test_a_non_numeric_match_id_is_rejected_without_work():
    module = main_module()
    interaction = _Interaction()
    _run(module.debrief_command(interaction, "not-a-match"))
    assert interaction.response.messages
    assert "not a match id" in interaction.response.messages[0]
    assert not interaction.response.deferred, "should reject before deferring"
    assert not interaction.followup.messages


def test_a_valid_id_defers_then_reports_through_the_followup():
    module = main_module()
    interaction = _Interaction()
    calls = []

    async def fake_on_demand(match_id):
        calls.append(match_id)
        return f"Posted the debrief for match {match_id} in 2 message(s)."

    original = module.bot.debrief_on_demand
    module.bot.debrief_on_demand = fake_on_demand
    try:
        _run(module.debrief_command(interaction, " 8123456789 "))
    finally:
        module.bot.debrief_on_demand = original

    assert calls == [8123456789], "whitespace should be tolerated"
    assert interaction.response.deferred, "building takes longer than the reply window"
    assert interaction.followup.messages == [
        "Posted the debrief for match 8123456789 in 2 message(s)."
    ]


def test_an_unexpected_failure_is_reported_not_raised():
    module = main_module()
    interaction = _Interaction()

    async def explode(match_id):
        raise KeyError("hero_totals")

    original = module.bot.debrief_on_demand
    module.bot.debrief_on_demand = explode
    try:
        _run(module.debrief_command(interaction, "1"))
    finally:
        module.bot.debrief_on_demand = original

    assert interaction.followup.messages
    assert "went wrong" in interaction.followup.messages[0]


def test_a_transient_failure_names_the_service_problem():
    module = main_module()
    interaction = _Interaction()

    async def flake(match_id):
        raise TimeoutError("opendota timed out")

    original = module.bot.debrief_on_demand
    module.bot.debrief_on_demand = flake
    try:
        _run(module.debrief_command(interaction, "1"))
    finally:
        module.bot.debrief_on_demand = original

    assert "service failed" in interaction.followup.messages[0]


def test_build_debrief_reports_why_it_could_not_build_one():
    module = main_module()

    async def no_match(session, match_id):
        return None

    original = module.fetch_parsed_match
    module.fetch_parsed_match = no_match
    try:
        text, failure = _run(module.build_debrief(None, None, 1, [], None, None))
    finally:
        module.fetch_parsed_match = original
    assert text is None
    assert failure == "OpenDota match data unavailable"
