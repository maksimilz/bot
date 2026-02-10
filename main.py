import asyncio
import os
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import ChatMemberUpdatedFilter, Command, IS_NOT_MEMBER, IS_MEMBER, MEMBER
from aiogram.types import ChatMemberUpdated, Message

from aiohttp import web
import gspread
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except (ValueError, TypeError):
    ADMIN_ID = 0
SHEET_NAME = "График подписчиков"


# Время отправки ежедневной сводки (по МСК)
DAILY_REPORT_HOUR = 9  # 09:00 утра
DAILY_REPORT_MINUTE = 0

# Глобальный кэш для таблицы
_SHEET_CACHE = None


# --- Инициализация Google Таблиц ---
def get_sheet(force_refresh=False):
    global _SHEET_CACHE
    if _SHEET_CACHE and not force_refresh:
        return _SHEET_CACHE

    creds_json = os.environ.get("G_SHEETS_KEY")
    if not creds_json:
        logging.error("❌ ОШИБКА: Нет ключа G_SHEETS_KEY в переменных окружения")
        return None
    
    try:
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
        sh = gc.open(SHEET_NAME)
        _SHEET_CACHE = sh.sheet1
        logging.info("✅ Подключение к Google Таблице обновлено")
        return _SHEET_CACHE
    except Exception as e:
        logging.error(f"❌ Ошибка подключения к Google Таблице: {e}")
        _SHEET_CACHE = None
        return None


# --- ДЕТЕКЦИЯ МАССОВЫХ ПОДПИСОК ---
class SurgeDetector:
    """Отслеживает всплески подписок за скользящее окно."""

    def __init__(
        self,
        window_seconds: int = 300,
        threshold: int = 10,
        cooldown_seconds: int = 300,
    ):
        self.window_seconds = window_seconds
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._timestamps: deque[float] = deque()
        self._last_alert_time: float = 0.0

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def record_and_check(self) -> tuple[bool, int]:
        """Записывает подписку и проверяет порог.
        Возвращает (нужен_ли_алерт, количество_за_окно)."""
        now = time.monotonic()
        self._timestamps.append(now)
        self._cleanup(now)
        count = len(self._timestamps)

        if count >= self.threshold and (now - self._last_alert_time) >= self.cooldown_seconds:
            self._last_alert_time = now
            return True, count
        return False, count


# Глобальный детектор — будет создан в main()
surge_detector: SurgeDetector | None = None

# Настройки surge detection
SURGE_WINDOW_SECONDS = 300   # 5 минут
SURGE_THRESHOLD = 10         # порог подписчиков
SURGE_COOLDOWN_SECONDS = 300 # не спамить алертами чаще чем раз в 5 минут


# --- ОЧЕРЕДЬ ЗАПИСИ В ТАБЛИЦУ ---
# Очередь будет создана в main(), чтобы избежать создания до запуска event loop
SHEET_QUEUE = None

MAX_RETRIES = 3  # количество попыток записи

async def process_sheet_queue():
    """Фоновая задача для пакетной записи в таблицу с ретраями"""
    while True:
        rows_to_write = []
        try:
            # Ждем первую запись
            first_item = await SHEET_QUEUE.get()
            rows_to_write.append(first_item)
            
            # Собираем остальные доступные записи в течение короткого времени
            try:
                while len(rows_to_write) < 50:
                    item = await asyncio.wait_for(SHEET_QUEUE.get(), timeout=2.0)
                    rows_to_write.append(item)
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                pass
            
            # Если есть что писать — пробуем с ретраями
            if rows_to_write:
                success = False
                for attempt in range(1, MAX_RETRIES + 1):
                    worksheet = get_sheet()
                    if not worksheet:
                        logging.error("❌ Не удалось получить таблицу, пробуем обновить кеш...")
                        worksheet = get_sheet(force_refresh=True)
                        if not worksheet:
                            logging.error(f"❌ Попытка {attempt}/{MAX_RETRIES}: таблица недоступна")
                            await asyncio.sleep(2 ** attempt)
                            continue
                    try:
                        await asyncio.to_thread(worksheet.append_rows, rows_to_write)
                        logging.info(f"✅ Записано пачкой: {len(rows_to_write)} строк")
                        success = True
                        break
                    except gspread.exceptions.APIError as e:
                        logging.error(f"❌ Попытка {attempt}/{MAX_RETRIES} — API ошибка: {e}")
                        # Сбрасываем кеш на случай протухшего токена
                        global _SHEET_CACHE
                        _SHEET_CACHE = None
                        await asyncio.sleep(2 ** attempt)
                    except Exception as e:
                        logging.error(f"❌ Попытка {attempt}/{MAX_RETRIES} — ошибка: {e}")
                        await asyncio.sleep(2 ** attempt)
                
                if not success:
                    logging.error(
                        f"🔴 ПОТЕРЯНО {len(rows_to_write)} строк после {MAX_RETRIES} попыток: "
                        f"{rows_to_write}"
                    )

        except Exception as e:
            logging.error(f"❌ Критическая ошибка в worker очереди: {e}")
            await asyncio.sleep(5)


# --- БОТ ---
router = Router()

def _tz():
    return ZoneInfo("Europe/Moscow")

# --- ФУНКЦИЯ ПОДСЧЕТА ПОДПИСЧИКОВ ЗА ДЕНЬ ---
async def send_daily_report(bot: Bot):
    """Отправляет админу статистику за вчерашний день"""
    if not ADMIN_ID:
        return
    
    worksheet = get_sheet()
    if not worksheet:
        await bot.send_message(ADMIN_ID, "❌ Не удалось получить данные из таблицы")

        return
    
    try:
        # Вчерашняя дата
        yesterday = (datetime.now(_tz()) - timedelta(days=1)).strftime("%d.%m.%Y")
        
        # Получаем список дат из первой колонки (асинхронно)
        # col_values(1) возвращает список значений первого столбца
        dates = await asyncio.to_thread(worksheet.col_values, 1)
        
        # Считаем подписчиков за вчерашний день. dates - это список строк
        yesterday_count = dates.count(yesterday)
        
        # Общее количество подписчиков (минус заголовок, если он есть)
        # Если список пуст, то 0. Если только заголовок, то 0.
        total_count = max(0, len(dates) - 1)

        
        # Формируем сообщение
        text = (
            f"📊 <b>Ежедневная сводка</b>\n\n"
            f"📅 Дата: {yesterday}\n"
            f"➕ Новых подписчиков: <b>{yesterday_count}</b>\n"
            f"👥 Всего подписчиков: <b>{total_count}</b>"
        )
        
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        logging.info(f"✅ Отправлена ежедневная сводка: {yesterday_count} подписчиков за {yesterday}")
        
    except Exception as e:
        logging.error(f"Ошибка при формировании ежедневной сводки: {e}")
        await bot.send_message(ADMIN_ID, f"❌ Ошибка при подсчете статистики: {e}")

@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot):
    """
    Отправляет статистику по запросу админа.
    
    ПЕРИОД ПОДСЧЕТА:
    - "Новых сегодня" считаются все подписчики, у которых в столбце "Дата" 
      указано текущее календарное число (формат дд.мм.гггг).
    - Используется московское время (MSK, GMT+3).
    - Период: с 00:00:00 до 23:59:59 текущего дня по МСК.
    """
    if message.from_user.id != ADMIN_ID:
        return

    worksheet = get_sheet()
    if not worksheet:
        await message.answer("❌ Не удалось получить данные из таблицы")
        return

    try:
        dates = await asyncio.to_thread(worksheet.col_values, 1)
        
        today = datetime.now(_tz()).strftime("%d.%m.%Y")
        today_count = dates.count(today)
        total_count = max(0, len(dates) - 1)
        
        # Текущий surge
        surge_info = ""
        if surge_detector:
            _, surge_count = surge_detector.record_and_check()
            # Убираем лишний тик, который мы только что добавили — это запрос статистики, не подписка
            surge_detector._timestamps.pop()
            surge_count = len(surge_detector._timestamps)
            if surge_count > 0:
                surge_info = f"\n⚡ За последние {SURGE_WINDOW_SECONDS // 60} мин: <b>{surge_count}</b>"
        
        text = (
            f"📊 <b>Текущая статистика</b>\n\n"
            f"📅 Сегодня: {today}\n"
            f"➕ Новых сегодня: <b>{today_count}</b>\n"
            f"👥 Всего подписчиков: <b>{total_count}</b>"
            f"{surge_info}"
        )
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logging.error(f"Ошибка команды /stats: {e}")
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Список доступных команд для админа."""
    if message.from_user.id != ADMIN_ID:
        return

    text = (
        "📋 <b>Доступные команды</b>\n\n"
        "/stats — Текущая статистика подписчиков\n"
        "/help — Этот список команд\n\n"
        "<i>Бот автоматически отслеживает подписки и отписки, "
        "записывает данные в Google Таблицу и отправляет "
        "ежедневную сводку.</i>\n\n"
        f"⚡ <i>Surge-алерт: ≥{SURGE_THRESHOLD} подписчиков "
        f"за {SURGE_WINDOW_SECONDS // 60} мин</i>"
    )
    await message.answer(text, parse_mode="HTML")

@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> MEMBER))
async def on_user_join(event: ChatMemberUpdated, bot: Bot):
    user = event.new_chat_member.user
    now = datetime.now(_tz())
    
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M:%S")
    full_name = user.full_name or "Без имени"
    username = f"@{user.username}" if user.username else ""
    user_id = str(user.id)

    logging.info(f"🔔 Новый подписчик: {full_name} ({user_id})")

    # Добавляем в очередь
    try:
        q_size = SHEET_QUEUE.qsize()
    except NotImplementedError:
        q_size = 0

    await SHEET_QUEUE.put([date_str, time_str, user_id, full_name, username, "➕ Подписка"])
    
    if q_size <= 5:
        sheet_status = "✅ Записано"
    else:
        sheet_status = "⏳ Добавлено в очередь записи"

    # Проверяем surge detection
    surge_text = ""
    if surge_detector:
        is_surge, surge_count = surge_detector.record_and_check()
        if is_surge:
            surge_text = (
                f"\n\n🚨 <b>ВСПЛЕСК ПОДПИСОК!</b>\n"
                f"⚡ {surge_count} подписчиков за последние "
                f"{SURGE_WINDOW_SECONDS // 60} мин!"
            )

    if ADMIN_ID:
        text = (
            f"🔔 <b>Новый подписчик!</b>\n"
            f"👤 {full_name} ({username})\n"
            f"🆔 <code>{user_id}</code>\n"
            f"📅 {date_str} {time_str}\n"
            f"<i>{sheet_status}</i>"
            f"{surge_text}"
        )
        try:
            await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось отправить ЛС админу: {e}")


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def on_user_leave(event: ChatMemberUpdated, bot: Bot):
    """Обработка отписки пользователя."""
    user = event.old_chat_member.user
    now = datetime.now(_tz())

    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M:%S")
    full_name = user.full_name or "Без имени"
    username = f"@{user.username}" if user.username else ""
    user_id = str(user.id)

    logging.info(f"👋 Отписка: {full_name} ({user_id})")

    await SHEET_QUEUE.put([date_str, time_str, user_id, full_name, username, "❌ Отписка"])

    if ADMIN_ID:
        text = (
            f"👋 <b>Отписка</b>\n"
            f"👤 {full_name} ({username})\n"
            f"🆔 <code>{user_id}</code>\n"
            f"📅 {date_str} {time_str}"
        )
        try:
            await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось отправить ЛС админу об отписке: {e}")

# --- ВЕБ-СЕРВЕР ---
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running with Google Sheets support"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    await asyncio.Event().wait()

# --- ЗАПУСК ---
async def main():
    global SHEET_QUEUE
    
    if not TOKEN:
        sys.exit("Ошибка: Не задан BOT_TOKEN")

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Создаём очередь после запуска event loop
    SHEET_QUEUE = asyncio.Queue()
    
    # Создаём surge detector
    surge_detector = SurgeDetector(
        window_seconds=SURGE_WINDOW_SECONDS,
        threshold=SURGE_THRESHOLD,
        cooldown_seconds=SURGE_COOLDOWN_SECONDS,
    )
    # Делаем глобально доступным
    import main as _self_module
    _self_module.surge_detector = surge_detector
    
    # Запускаем обработчик очереди в фоне
    asyncio.create_task(process_sheet_queue())

    # Настраиваем планировщик для ежедневной сводки

    scheduler = AsyncIOScheduler(timezone=str(_tz()))
    scheduler.add_job(
        send_daily_report,
        'cron',
        hour=DAILY_REPORT_HOUR,
        minute=DAILY_REPORT_MINUTE,
        args=[bot]
    )
    scheduler.start()
    logging.info(f"⏰ Планировщик запущен. Ежедневная сводка в {DAILY_REPORT_HOUR:02d}:{DAILY_REPORT_MINUTE:02d} МСК")

    # При старте проверим связь с таблицей
    if ADMIN_ID:
        try:
            sheet = get_sheet()
            if sheet:
                await bot.send_message(ADMIN_ID, "🤖 Бот перезапущен. 🟢 Связь с Google Таблицей: ОК\n⏰ Ежедневные сводки активированы\n📦 Пакетная запись включена")
            else:
                await bot.send_message(ADMIN_ID, "🤖 Бот перезапущен. 🔴 ОШИБКА доступа к Таблице (см. логи)")
        except Exception as e:
            logging.error(f"Ошибка старта: {e}")

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
