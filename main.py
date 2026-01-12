import asyncio
import os
import json
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, MEMBER
from aiogram.types import ChatMemberUpdated
from aiohttp import web
import gspread

# --- НАСТРОЙКИ ---
# Токен и ID админа берем из переменных окружения
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID") # Render хранит как строку, преобразуем ниже

# ИМЯ ВАШЕЙ ТАБЛИЦЫ (должно совпадать буква в букву с названием в Google)
SHEET_NAME = "График подписчиков" 

# --- Инициализация Google Таблиц ---
def get_sheet():
    creds_json = os.environ.get("G_SHEETS_KEY")
    if not creds_json:
        logging.error("❌ ОШИБКА: Нет ключа G_SHEETS_KEY в переменных окружения")
        return None
    
    try:
        creds_dict = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds_dict)
        # Открываем таблицу по имени
        sh = gc.open(SHEET_NAME)
        # Возвращаем первый лист
        return sh.sheet1
    except Exception as e:
        logging.error(f"❌ Ошибка подключения к Google Таблице: {e}")
        return None

# --- БОТ ---
router = Router()

def _tz():
    # Московское время
    return ZoneInfo("Europe/Moscow")

@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> MEMBER))
async def on_user_join(event: ChatMemberUpdated, bot: Bot):
    user = event.new_chat_member.user
    now = datetime.now(_tz())
    
    # Данные подписчика
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M:%S")
    full_name = user.full_name or "Без имени"
    username = f"@{user.username}" if user.username else ""
    user_id = str(user.id)

    logging.info(f"🔔 Новый подписчик: {full_name} ({user_id})")

    # 1. Пробуем записать в Google Таблицу
    sheet_status = "❌ Не записано в таблицу"
    worksheet = get_sheet()
    if worksheet:
        try:
            # Добавляем строку: Дата | Время | ID | Имя | Username
            worksheet.append_row([date_str, time_str, user_id, full_name, username])
            sheet_status = "✅ Сохранено в Google Таблицу"
        except Exception as e:
            logging.error(f"Ошибка записи в таблицу: {e}")
            sheet_status = f"❌ Ошибка записи: {e}"
    else:
        sheet_status = "❌ Таблица не найдена или нет доступа"

    # 2. Шлем уведомление админу в личку
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

# --- ВЕБ-СЕРВЕР (Для Render) ---
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running with Google Sheets support"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    # Держим процесс живым
    await asyncio.Event().wait()

# --- ЗАПУСК ---
async def main():
    if not TOKEN:
        sys.exit("Ошибка: Не задан BOT_TOKEN")

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # При старте проверим связь с таблицей и сообщим админу
    if ADMIN_ID:
        try:
            sheet = get_sheet()
            if sheet:
                await bot.send_message(int(ADMIN_ID), "🤖 Бот перезапущен. 🟢 Связь с Google Таблицей: ОК")
            else:
                await bot.send_message(int(ADMIN_ID), "🤖 Бот перезапущен. 🔴 ОШИБКА доступа к Таблице (см. логи)")
        except Exception as e:
            logging.error(f"Ошибка старта: {e}")

    # Запускаем сервер и бота параллельно
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
