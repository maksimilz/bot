import asyncio
import logging
import sys
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    TOKEN,
    ADMIN_ID,
    DAILY_REPORT_HOUR,
    DAILY_REPORT_MINUTE,
    SURGE_WINDOW_SECONDS,
    SURGE_THRESHOLD,
    SURGE_COOLDOWN_SECONDS,
)
import state
from surge import SurgeDetector
from sheets import get_sheet, process_sheet_queue
from handlers import router, send_daily_report
from web_server import start_web_server


def _tz():
    return ZoneInfo("Europe/Moscow")


async def main():
    if not TOKEN:
        sys.exit("Ошибка: Не задан BOT_TOKEN")

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Инициализируем глобальное состояние
    state.sheet_queue = asyncio.Queue()
    state.surge_detector = SurgeDetector(
        window_seconds=SURGE_WINDOW_SECONDS,
        threshold=SURGE_THRESHOLD,
        cooldown_seconds=SURGE_COOLDOWN_SECONDS,
    )

    # Запускаем обработчик очереди в фоне
    asyncio.create_task(process_sheet_queue(state.sheet_queue))

    # Настраиваем планировщик для ежедневной сводки
    scheduler = AsyncIOScheduler(timezone=str(_tz()))
    scheduler.add_job(
        send_daily_report,
        'cron',
        hour=DAILY_REPORT_HOUR,
        minute=DAILY_REPORT_MINUTE,
        args=[bot],
    )
    scheduler.start()
    logging.info(
        f"⏰ Планировщик запущен. Ежедневная сводка в "
        f"{DAILY_REPORT_HOUR:02d}:{DAILY_REPORT_MINUTE:02d} МСК"
    )

    # При старте проверим связь с таблицей
    if ADMIN_ID:
        try:
            sheet = get_sheet()
            if sheet:
                await bot.send_message(
                    ADMIN_ID,
                    "🤖 Бот перезапущен. 🟢 Связь с Google Таблицей: ОК\n"
                    "⏰ Ежедневные сводки активированы\n"
                    "📦 Пакетная запись включена",
                )
            else:
                await bot.send_message(
                    ADMIN_ID,
                    "🤖 Бот перезапущен. 🔴 ОШИБКА доступа к Таблице (см. логи)",
                )
        except Exception as e:
            logging.error(f"Ошибка старта: {e}")

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
