import asyncio
import os

from aiohttp import web


async def start_web_server():
    """Health-check HTTP сервер (для UptimeRobot / cron-job и Render)."""
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running with Google Sheets support"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    await asyncio.Event().wait()
