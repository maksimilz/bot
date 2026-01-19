import asyncio
import os
import json
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, MEMBER
from aiogram.types import ChatMemberUpdated
from aiohttp import web
import gspread
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")
SHEET_NAME = "График подписчиков"

# Время отправки ежедневной сводки (по МСК)
DAILY_REPORT_HOUR = 9  # 09:00 утра
DAILY_REPORT_MINUTE = 0

# --- Инициализация Google Таблиц ---
def get_sheet():
    creds_json = os.environ.get("G_SHEETS_KEY")
    if not creds_json:
        logging.error("❌ ОШИБКА: Нет ключа G_SHEETS_KEY в переменных окружения")
        return None
    
    try:
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
        sh = gc.open(SHEET_NAME)
        return sh.sheet1
    except Exception as e:
        logging.error(f"❌ Ошибка подключения к Google Таблице: {e}")
        return None

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
        await bot.send_message(int(ADMIN_ID), "❌ Не удалось получить данные из таблицы")
        return
    
    try:
        # Вчерашняя дата
        yesterday = (datetime.now(_tz()) - timedelta(days=1)).strftime("%d.%m.%Y")
        
        # Получаем все строки из таблицы
        all_records = worksheet.get_all_records()
        
        # Считаем подписчиков за вчерашний день
        yesterday_count = sum(1 for record in all_records if record.get('Дата') == yesterday)
        
        # Общее количество подписчиков
        total_count = len(all_records)
        
        # Формируем сообщение
        text = (
            f"📊 <b>Ежедневная сводка</b>\n\n"
            f"📅 Дата: {yesterday}\n"
            f"➕ Новых подписчиков: <b>{yesterday_count}</b>\n"
            f"👥 Всего подписчиков: <b>{total_count}</b>"
        )
        
        await bot.send_message(int(ADMIN_ID), text, parse_mode="HTML")
        logging.info(f"✅ Отправлена ежедневная сводка: {yesterday_count} подписчиков за {yesterday}")
        
    except Exception as e:
        logging.error(f"Ошибка при формировании ежедневной сводки: {e}")
        await bot.send_message(int(ADMIN_ID), f"❌ Ошибка при подсчете статистики: {e}")

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

    sheet_status = "❌ Не записано в таблицу"
    worksheet = get_sheet()
    if worksheet:
        try:
            worksheet.append_row([date_str, time_str, user_id, full_name, username])
            sheet_status = "✅ Сохранено в Google Таблицу"
        except Exception as e:
            logging.error(f"Ошибка записи в таблицу: {e}")
            sheet_status = f"❌ Ошибка записи: {e}"
    else:
        sheet_status = "❌ Таблица не найдена или нет доступа"

    if ADMIN_ID:
        text = (
            f"🔔 <b>Новый подписчик!</b>\n"
            f"👤 {full_name} ({username})\n"
            f"🆔 <code>{user_id}</code>\n"
            f"📅 {date_str} {time_str}\n"
            f"<i>{sheet_status}</i>"
        )
        try:
            await bot.send_message(int(ADMIN_ID), text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось отправить ЛС админу: {e}")

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
    if not TOKEN:
        sys.exit("Ошибка: Не задан BOT_TOKEN")

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

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
                await bot.send_message(int(ADMIN_ID), "🤖 Бот перезапущен. 🟢 Связь с Google Таблицей: ОК\n⏰ Ежедневные сводки активированы")
            else:
                await bot.send_message(int(ADMIN_ID), "🤖 Бот перезапущен. 🔴 ОШИБКА доступа к Таблице (см. логи)")
        except Exception as e:
            logging.error(f"Ошибка старта: {e}")

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
