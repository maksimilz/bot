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
    SURGE_QUIET_PERIOD,
    SURGE_UPDATE_INTERVAL,
    PERIODIC_REPORT_HOURS,
)
import state
from surge import SurgeDetector, WaveAction
from sheets import get_sheet, process_sheet_queue
from handlers import router, send_daily_report, send_periodic_report
from web_server import start_web_server


def _tz():
    return ZoneInfo("Europe/Moscow")


async def check_wave_status(bot: Bot):
    """Периодическая проверка: не завершилась ли волна подписок."""
    if not state.surge_detector or not ADMIN_ID:
        return

    action, wave_info = state.surge_detector.check_wave()

    if action == WaveAction.NONE or wave_info is None:
        return

    if action == WaveAction.SEND_SUMMARY:
        net_str = f"+{wave_info.net}" if wave_info.net >= 0 else str(wave_info.net)
        text = (
            f"📊 <b>Волна завершена!</b>\n"
            f"⚡ Итого: <b>+{wave_info.joins}</b> подписок / "
            f"<b>-{wave_info.leaves}</b> отписок\n"
            f"⏱ Длительность: <b>{wave_info.duration_minutes} мин</b>\n"
            f"📈 Чистый прирост: <b>{net_str}</b>"
        )
    elif action == WaveAction.SEND_UPDATE:
        text = (
            f"🔥 <b>Волна продолжается!</b>\n"
            f"⚡ Уже <b>+{wave_info.joins}</b> подписок / "
            f"<b>-{wave_info.leaves}</b> отписок\n"
            f"⏱ Идёт <b>{wave_info.duration_minutes} мин</b>..."
        )
    else:
        return

    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        logging.info(f"📊 Wave alert ({action.value}): +{wave_info.joins}/-{wave_info.leaves}")
    except Exception as e:
        logging.error(f"Ошибка отправки wave-алерта: {e}")


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
        quiet_period=SURGE_QUIET_PERIOD,
        update_interval=SURGE_UPDATE_INTERVAL,
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
    scheduler.add_job(
        send_periodic_report, 'interval',
        hours=PERIODIC_REPORT_HOURS,
        args=[bot],
    )
    scheduler.add_job(
        check_wave_status, 'interval',
        seconds=30,
        args=[bot],
    )
    scheduler.start()
    logging.info(
        f"⏰ Планировщик запущен. Ежедневная сводка в "
        f"{DAILY_REPORT_HOUR:02d}:{DAILY_REPORT_MINUTE:02d} МСК, "
        f"мини-сводка каждые {PERIODIC_REPORT_HOURS}ч"
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
