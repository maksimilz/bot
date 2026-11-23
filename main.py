import asyncio
import logging
import signal
import sys
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    TOKEN,
    ADMIN_ID,
    SURGE_WINDOW_SECONDS,
    SURGE_THRESHOLD,
    SURGE_COOLDOWN_SECONDS,
    SURGE_QUIET_PERIOD,
    SURGE_UPDATE_INTERVAL,
)
import state
from surge import SurgeDetector, WaveAction
from sheets import get_stats_sheet
from handlers import reset_daily_stats, router
from web_server import start_web_server
from llm import close_llm_session


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
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML", disable_notification=True)
        logging.info(f"📊 Wave alert ({action.value}): +{wave_info.joins}/-{wave_info.leaves}")
    except Exception as e:
        logging.error(f"Ошибка отправки wave-алерта: {e}")


async def register_post_menu(bot: Bot):
    if ADMIN_ID <= 0:
        return

    admin_scope = BotCommandScopeChat(chat_id=ADMIN_ID)
    scopes = (admin_scope, BotCommandScopeAllPrivateChats(), BotCommandScopeDefault())

    try:
        default_commands = []
        russian_commands = []
        for scope in scopes:
            localized = await bot.get_my_commands(scope=scope, language_code="ru") if not russian_commands else []
            common = await bot.get_my_commands(scope=scope) if not default_commands else []
            russian_commands = russian_commands or localized or common
            default_commands = default_commands or common
            if russian_commands and default_commands:
                break

        post = BotCommand(command="post", description="Подготовить пост для соцсетей")
        for language_code, commands in ((None, default_commands), ("ru", russian_commands)):
            if not any(command.command == post.command for command in commands):
                await bot.set_my_commands(
                    [*commands, post], scope=admin_scope, language_code=language_code
                )
                logging.info("Команда /post добавлена в меню администратора (%s)", language_code or "default")
    except TelegramAPIError as error:
        logging.warning("Не удалось обновить меню команд администратора: %s", error)


async def main():
    if not TOKEN:
        sys.exit("Ошибка: Не задан BOT_TOKEN")

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(register_post_menu)

    # Инициализируем глобальное состояние
    state.surge_detector = SurgeDetector(
        window_seconds=SURGE_WINDOW_SECONDS,
        threshold=SURGE_THRESHOLD,
        cooldown_seconds=SURGE_COOLDOWN_SECONDS,
        quiet_period=SURGE_QUIET_PERIOD,
        update_interval=SURGE_UPDATE_INTERVAL,
    )

    # Восстанавливаем счётчики из файла (если бот перезапускался)
    state.load_counters()

    # Инициализируем лист «Статистика» (создаёт если не существует, считает seed)
    await asyncio.to_thread(get_stats_sheet)

    # Настраиваем запись статистики и проверку всплесков
    scheduler = AsyncIOScheduler(timezone=str(_tz()))
    scheduler.add_job(
        reset_daily_stats,
        'cron',
        hour=0,
        minute=0,
    )
    scheduler.add_job(
        check_wave_status, 'interval',
        seconds=30,
        args=[bot],
    )
    scheduler.add_job(
        state.save_counters, 'interval',
        minutes=5,
    )
    scheduler.start()
    logging.info("⏰ Планировщик запущен. Запись статистики в 00:00 МСК")

    # --- Graceful shutdown ---
    shutdown_event = asyncio.Event()

    async def graceful_shutdown():
        """Корректное завершение."""
        logging.info("🛑 Получен сигнал завершения, начинаем graceful shutdown...")

        # 1. Останавливаем polling
        await dp.stop_polling()
        logging.info("  ⏹ Polling остановлен")

        # 2. Останавливаем планировщик
        scheduler.shutdown(wait=False)
        logging.info("  ⏹ Планировщик остановлен")

        # 3. Сохраняем счётчики перед выходом
        state.save_counters()
        logging.info("  💾 Счётчики сохранены")

        # 4. Закрываем LLM-сессию
        await close_llm_session()
        logging.info("  ⏹ LLM-сессия закрыта")

        # 5. Останавливаем web-сервер
        shutdown_event.set()
        logging.info("  ⏹ Web-сервер остановлен")

        # 6. Уведомляем админа
        if ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    "🔄 Старая версия бота отключена (graceful shutdown при обновлении)",
                    disable_notification=True
                )
            except Exception:
                pass

        # 7. Закрываем сессию бота
        await bot.session.close()
        logging.info("✅ Graceful shutdown завершён")

    def _signal_handler():
        asyncio.ensure_future(graceful_shutdown())

    # Регистрируем обработчики сигналов (кроссплатформенно)
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)
    else:
        # На Windows используем signal.signal (менее надёжно, но работает)
        def _win_handler(signum, frame):
            loop.call_soon_threadsafe(_signal_handler)
        signal.signal(signal.SIGTERM, _win_handler)
        signal.signal(signal.SIGINT, _win_handler)

    # При старте проверим связь с таблицей
    if ADMIN_ID:
        try:
            sheet_ok = await asyncio.to_thread(get_stats_sheet)
            status = "🟢 Лист 'Статистика': ОК" if sheet_ok else "🔴 ОШИБКА доступа к Таблице"
            await bot.send_message(
                ADMIN_ID,
                f"🤖 Бот перезапущен.\n"
                f"{status}\n"
                f"📊 Хранение: агрегаты по дням\n"
                f"🛡 Graceful shutdown активен",
                disable_notification=True,
            )
        except Exception as e:
            logging.error(f"Ошибка старта: {e}")

    # Сбрасываем вебхук/старые соединения при старте (устраняет TelegramConflictError)
    await bot.delete_webhook(drop_pending_updates=False)

    await asyncio.gather(
        start_web_server(shutdown_event),
        dp.start_polling(bot, handle_signals=False),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())

