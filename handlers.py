import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import ChatMemberUpdatedFilter, Command, IS_NOT_MEMBER, IS_MEMBER, MEMBER
from aiogram.types import ChatMemberUpdated, Message

from config import (
    ADMIN_ID,
    SURGE_WINDOW_SECONDS,
    SURGE_THRESHOLD,
    SYSTEM_PROMPT,
)
import config
from sheets import get_sheet
from llm import ask_llm
import state


router = Router()


def _tz():
    return ZoneInfo("Europe/Moscow")


# --- ЕЖЕДНЕВНАЯ СВОДКА ---
async def send_daily_report(bot: Bot):
    """Отправляет админу статистику за вчерашний день и текущее утро."""
    if not ADMIN_ID:
        return

    worksheet = get_sheet()
    if not worksheet:
        await bot.send_message(ADMIN_ID, "❌ Не удалось получить данные из таблицы")
        return

    try:
        now = datetime.now(_tz())
        yesterday = (now - timedelta(days=1)).strftime("%d.%m.%Y")
        today = now.strftime("%d.%m.%Y")
        
        dates = await asyncio.to_thread(worksheet.col_values, 1)

        yesterday_count = dates.count(yesterday)
        today_count = dates.count(today)
        total_count = max(0, len(dates) - 1)

        text = (
            f"📊 <b>Ежедневная сводка</b>\n\n"
            f"🗓 <b>Вчера ({yesterday}):</b> {yesterday_count}\n"
            f"🌤 <b>Сегодня ({today}):</b> {today_count}\n"
            f"👥 <b>Всего:</b> {total_count}"
        )

        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        logging.info(f"✅ Отправлена ежедневная сводка. Вчера: {yesterday_count}, Сегодня: {today_count}")

    except Exception as e:
        logging.error(f"Ошибка при формировании ежедневной сводки: {e}")
        await bot.send_message(ADMIN_ID, f"❌ Ошибка при подсчете статистики: {e}")


# --- КОМАНДЫ ---
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
        if state.surge_detector:
            _, surge_count = state.surge_detector.record_and_check()
            # Убираем лишний тик — это запрос статистики, не подписка
            state.surge_detector._timestamps.pop()
            surge_count = len(state.surge_detector._timestamps)
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
        "/ask — Задать вопрос ИИ (помнит контекст)\n"
        "/new — Новый диалог (очистить память ИИ)\n"
        "/help — Этот список команд\n\n"
        "<i>Бот автоматически отслеживает подписки и отписки, "
        "записывает данные в Google Таблицу и отправляет "
        "ежедневную сводку.</i>\n\n"
        f"⚡ <i>Surge-алерт: ≥{SURGE_THRESHOLD} подписчиков "
        f"за {SURGE_WINDOW_SECONDS // 60} мин</i>"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("new"))
async def cmd_new(message: Message):
    """Очистить память диалога с ИИ."""
    if message.from_user.id != ADMIN_ID:
        return

    user_id = message.from_user.id
    if user_id in state.messages_history:
        state.messages_history[user_id].clear()
    await message.answer("🗑 Память очищена. Новый диалог начат!")


@router.message(Command("ask"))
async def cmd_ask(message: Message):
    """Задать вопрос ИИ через OpenRouter с памятью контекста (10 сообщений)."""
    if message.from_user.id != ADMIN_ID:
        return

    user_id = message.from_user.id
    question = message.text.replace("/ask", "", 1).strip()
    
    if not question:
        await message.answer("✏️ Напиши вопрос после /ask\n\nПример: /ask Какая погода в Москве?")
        return

    # Инициализация истории для пользователя
    if user_id not in state.messages_history:
        state.messages_history[user_id] = deque(maxlen=10)
    
    history = state.messages_history[user_id]
    history.append({"role": "user", "content": question})

    await message.answer("🤔 Думаю...")

    # Отправляем всю историю диалога с системным промптом
    system_msg = {"role": "system", "content": SYSTEM_PROMPT}
    answer = await ask_llm(
        messages=[system_msg] + list(history),
    )

    if answer:
        history.append({"role": "assistant", "content": answer})
        await message.answer(answer)
    else:
        # Если ошибка, удаляем последний вопрос из истории, чтобы не засорять
        if history and history[-1]["role"] == "user":
            history.pop()
        await message.answer(f"❌ Не удалось получить ответ от LLM. Проверь баланс/ключ.\nМодель: {config.OPENROUTER_MODEL}")


# --- СОБЫТИЯ ПОДПИСКИ / ОТПИСКИ ---
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
        q_size = state.sheet_queue.qsize()
    except NotImplementedError:
        q_size = 0

    await state.sheet_queue.put([date_str, time_str, user_id, full_name, username, "➕ Подписка"])

    if q_size <= 5:
        sheet_status = "✅ Записано"
    else:
        sheet_status = "⏳ Добавлено в очередь записи"

    # Проверяем surge detection
    surge_text = ""
    if state.surge_detector:
        is_surge, surge_count = state.surge_detector.record_and_check()
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

    await state.sheet_queue.put([date_str, time_str, user_id, full_name, username, "❌ Отписка"])

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
