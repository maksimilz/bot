import asyncio
import os

from aiohttp import web


async def start_web_server(shutdown_event: asyncio.Event | None = None):
    """Health-check HTTP сервер (для UptimeRobot / cron-job и Render)."""
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running with Google Sheets support"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # Ждём сигнала завершения (или бесконечно, если event не передан)
    if shutdown_event:
        await shutdown_event.wait()
    else:
        await asyncio.Event().wait()

    await runner.cleanup()
