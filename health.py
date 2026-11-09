"""A health endpoint, so this can run as a web service.

Render expects a process that binds $PORT and answers on /. The bot itself does
not need it; without it the service is reaped.
"""
import os

from aiohttp import web

_RUNNER = None


async def handle_ping(request):
    return web.Response(text="Dota 2 Coaching Bot is Online!")


async def start_web_server():
    """Start the health endpoint once during bot startup."""
    global _RUNNER
    if _RUNNER is not None:
        return

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    _RUNNER = runner
    print(f"[SERVER] Web server successfully bound to port {port}", flush=True)
