"""Test support: import main.py without its runtime dependencies installed.

discord.py and aiohttp are not needed to exercise this project's logic, and
requiring them would make the suite unrunnable wherever pip is unavailable.
Both are replaced with stubs. The aiohttp stub serves canned OpenDota responses
from tests/fixtures, so no test touches the network.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

_main = None


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def match_without_teamfights():
    """The parsed fixture with its teamfight log emptied.

    Derived rather than stored: it differs from match_parsed.json by one field,
    and a near-duplicate 137 KB fixture would hide that.
    """
    data = fixture("match_parsed.json")
    data["teamfights"] = []
    return data


def match_without_full_five_v_fives():
    """The parsed fixture with every fight short of ten participants.

    A distinct branch from an empty log: telemetry exists, but no fight is a
    full 5v5, so the summary is available with a zero count. The 5v5 review is
    the feature built on this data, and this is the path where it has nothing
    to say.
    """
    data = fixture("match_parsed.json")
    for fight in data.get("teamfights") or []:
        fight["players"] = (fight.get("players") or [])[:9]
    return data


def _constants_routes():
    data = fixture("constants.json")
    return {
        "constants/patch": data["patch"],
        "constants/hero_abilities": data["hero_abilities"],
        "constants/abilities": data["abilities"],
        "constants/items": data["items"],
        "api/heroes": data["heroes"],
    }


def _install_stubs():
    routes = _constants_routes()

    aiohttp = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    class ContentTypeError(ClientError):
        pass

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class _Response:
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload

        async def json(self):
            return self._payload

        async def text(self):
            return json.dumps(self._payload)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class ClientSession:
        """Serves fixture data for known URLs and 404s everything else."""

        def __init__(self, timeout=None, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            payload, status = None, 404
            for marker, value in routes.items():
                if marker in url:
                    payload, status = value, 200
                    break
            return _Response(status, payload)

        def post(self, url, **kwargs):
            return _Response(200, {})

    aiohttp.ClientError = ClientError
    aiohttp.ContentTypeError = ContentTypeError
    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientSession = ClientSession

    web = types.ModuleType("aiohttp.web")
    for name in ("Response", "Application", "AppRunner", "TCPSite"):
        setattr(web, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    aiohttp.web = web
    sys.modules["aiohttp"] = aiohttp
    sys.modules["aiohttp.web"] = web

    discord = types.ModuleType("discord")

    class _Intents:
        @staticmethod
        def default():
            return types.SimpleNamespace(message_content=False)

    discord.Intents = _Intents
    discord.Embed = type("Embed", (), {"__init__": lambda self, **k: None})
    discord.Color = types.SimpleNamespace(purple=lambda: None)
    discord.DiscordException = type("DiscordException", (Exception,), {})

    ext = types.ModuleType("discord.ext")
    commands = types.ModuleType("discord.ext.commands")

    class Bot:
        def __init__(self, *a, **k):
            pass

        def event(self, fn):
            return fn

    commands.Bot = Bot

    tasks = types.ModuleType("discord.ext.tasks")

    def loop(**kwargs):
        def decorate(fn):
            fn.is_running = lambda: False
            fn.start = lambda: None
            return fn
        return decorate

    tasks.loop = loop
    ext.commands = commands
    ext.tasks = tasks
    discord.ext = ext
    for name, module in (("discord", discord), ("discord.ext", ext),
                         ("discord.ext.commands", commands),
                         ("discord.ext.tasks", tasks)):
        sys.modules[name] = module


def main_module():
    """Import main.py once, with stubs in place, and cache it."""
    global _main
    if _main is None:
        _install_stubs()
        spec = importlib.util.spec_from_file_location(
            "bot_main", os.path.join(ROOT, "main.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["bot_main"] = module
        spec.loader.exec_module(module)
        _main = module
    return _main


def state_module():
    """The queue module, with stubs already installed by importing main."""
    main_module()
    import state
    return state


def with_constants():
    """Return main.py with its game-data maps loaded from the fixture."""
    module = main_module()
    if not module.HERO_NAME_MAP:
        asyncio.run(module.load_hero_constants(force=True))
    return module
