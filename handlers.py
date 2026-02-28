import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import ChatMemberUpdatedFilter, Command, IS_NOT_MEMBER, IS_MEMBER, MEMBER, KICKED
from aiogram.types import ChatMemberUpdated, Message

from config import (
    ADMIN_ID,
    SURGE_WINDOW_SECONDS,
    SURGE_THRESHOLD,
    PERIODIC_REPORT_HOURS,
    SYSTEM_PROMPT,
)
import config
from sheets import get_sheet
from llm import ask_llm
from web_search import search_web, format_search_context
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
        
        all_rows = await asyncio.to_thread(worksheet.get_all_values)
        data_rows = all_rows[1:]  # Пропускаем заголовок

        y_joins = sum(1 for r in data_rows if r[0] == yesterday and len(r) >= 6 and "Подписка" in r[5])
        y_leaves = sum(1 for r in data_rows if r[0] == yesterday and len(r) >= 6 and "Отписка" in r[5])
        t_joins = sum(1 for r in data_rows if r[0] == today and len(r) >= 6 and "Подписка" in r[5])
        t_leaves = sum(1 for r in data_rows if r[0] == today and len(r) >= 6 and "Отписка" in r[5])
        total_count = len(data_rows)

        y_net = y_joins - y_leaves
        y_net_str = f"+{y_net}" if y_net >= 0 else str(y_net)
        t_net = t_joins - t_leaves
        t_net_str = f"+{t_net}" if t_net >= 0 else str(t_net)

        text = (
            f"📊 <b>Ежедневная сводка</b>\n\n"
            f"🗓 <b>Вчера ({yesterday}):</b>\n"
            f"   ➕ {y_joins}  ➖ {y_leaves}  (итого {y_net_str})\n\n"
            f"🌤 <b>Сегодня ({today}):</b>\n"
            f"   ➕ {t_joins}  ➖ {t_leaves}  (итого {t_net_str})\n\n"
            f"👥 <b>Всего записей:</b> {total_count}"
        )

        await bot.send_message(ADMIN_ID, text, parse_mode="HTML", disable_notification=True)
        logging.info(f"✅ Отправлена ежедневная сводка. Вчера: {y_net_str}, Сегодня: {t_net_str}")

    except Exception as e:
        logging.error(f"Ошибка при формировании ежедневной сводки: {e}")
        await bot.send_message(ADMIN_ID, f"❌ Ошибка при подсчете статистики: {e}")


async def send_periodic_report(bot: Bot):
    """Мини-сводка каждые N часов. Молчит, если событий не было."""
    if not ADMIN_ID:
        return

    # Атомарно читаем и сбрасываем счётчики
    joins = state.periodic_joins.reset()
    leaves = state.periodic_leaves.reset()

    if joins == 0 and leaves == 0:
        return  # Нет событий — молчим

    net = joins - leaves
    net_str = f"+{net}" if net >= 0 else str(net)

    text = (
        f"📊 <b>Сводка за {PERIODIC_REPORT_HOURS}ч</b>\n\n"
        f"➕ Подписок: <b>{joins}</b>\n"
        f"➖ Отписок: <b>{leaves}</b>\n"
        f"📈 Чистый рост: <b>{net_str}</b>"
    )

    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML", disable_notification=True)
        logging.info(f"📊 Периодическая сводка: +{joins} / -{leaves}")
    except Exception as e:
        logging.error(f"Ошибка отправки периодической сводки: {e}")


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
        all_rows = await asyncio.to_thread(worksheet.get_all_values)
        data_rows = all_rows[1:]  # Пропускаем заголовок

        today = datetime.now(_tz()).strftime("%d.%m.%Y")
        today_joins = sum(1 for r in data_rows if r[0] == today and len(r) >= 6 and "Подписка" in r[5])
        today_leaves = sum(1 for r in data_rows if r[0] == today and len(r) >= 6 and "Отписка" in r[5])
        total_count = len(data_rows)
        net = today_joins - today_leaves
        net_str = f"+{net}" if net >= 0 else str(net)

        # Текущий surge
        surge_info = ""
        if state.surge_detector:
            joins_w, leaves_w = state.surge_detector.get_counts()
            if joins_w > 0 or leaves_w > 0:
                surge_info = (
                    f"\n⚡ За последние {SURGE_WINDOW_SECONDS // 60} мин: "
                    f"<b>+{joins_w} / -{leaves_w}</b>"
                )

        text = (
            f"📊 <b>Текущая статистика</b>\n\n"
            f"📅 Сегодня: {today}\n"
            f"➕ Подписок: <b>{today_joins}</b>\n"
            f"➖ Отписок: <b>{today_leaves}</b>\n"
            f"📈 Чистый рост: <b>{net_str}</b>\n"
            f"👥 Всего записей: <b>{total_count}</b>"
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
        "/search — Поиск в интернете через ИИ\n"
        "/new — Новый диалог (очистить память ИИ)\n"
        "/help — Этот список команд\n\n"
        "<i>Бот автоматически отслеживает подписки и отписки, "
        "записывает данные в Google Таблицу.</i>\n\n"
        "📬 <b>Уведомления:</b>\n"
        f"⚡ Surge-алерт: ≥{SURGE_THRESHOLD} подписок "
        f"за {SURGE_WINDOW_SECONDS // 60} мин\n"
        f"📊 Мини-сводка: каждые {PERIODIC_REPORT_HOURS}ч\n"
        "📅 Ежедневная сводка: 09:00 МСК"
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


@router.message(Command("search"))
async def cmd_search(message: Message):
    """Поиск в интернете через DuckDuckGo + ответ от LLM."""
    if message.from_user.id != ADMIN_ID:
        return

    query = message.text.replace("/search", "", 1).strip()

    if not query:
        await message.answer(
            "🔍 Напиши запрос после /search\n\n"
            "Пример: /search последние новости Python"
        )
        return

    await message.answer("🔍 Ищу в интернете...")

    # Поиск в DuckDuckGo
    results = await search_web(query, max_results=5)

    if not results:
        await message.answer("❌ Ничего не найдено. Попробуй другой запрос.")
        return

    # Формируем контекст для LLM
    search_context = format_search_context(results)

    system_msg = {
        "role": "system",
        "content": (
            f"{SYSTEM_PROMPT}\n\n"
            "Тебе предоставлены результаты поиска в интернете. "
            "Ответь на основе найденного, указывая источники (ссылки), если уместно."
        ),
    }
    user_msg = {
        "role": "user",
        "content": f"Запрос: {query}\n\nРезультаты поиска:\n{search_context}",
    }

    answer = await ask_llm(messages=[system_msg, user_msg], max_tokens=1500)

    if answer:
        await message.answer(answer)
    else:
        # Если LLM не ответил, покажем сырые результаты
        fallback = "🔍 <b>Результаты поиска:</b>\n\n"
        for r in results[:3]:
            title = r.get("title", "")
            url = r.get("href", "")
            snippet = r.get("body", "")
            fallback += f"📌 <b>{title}</b>\n{snippet}\n{url}\n\n"
        await message.answer(fallback, parse_mode="HTML")


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

    # Запись в Google Sheets
    await state.sheet_queue.put([date_str, time_str, user_id, full_name, username, "➕ Подписка"])

    # Счётчик для периодической сводки
    state.periodic_joins.increment()

    # Surge detection — алерт только при всплеске
    if state.surge_detector:
        is_surge, joins_w, leaves_w = state.surge_detector.record_join()
        if is_surge and ADMIN_ID:
            text = (
                f"🚨 <b>ВСПЛЕСК ПОДПИСОК!</b>\n"
                f"⚡ +{joins_w} подписок / -{leaves_w} отписок "
                f"за последние {SURGE_WINDOW_SECONDS // 60} мин!"
            )
            try:
                await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Не удалось отправить surge-алерт: {e}")


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> (IS_NOT_MEMBER | KICKED)))
async def on_user_leave(event: ChatMemberUpdated, bot: Bot):
    """Обработка отписки пользователя (в т.ч. кик)."""
    user = event.old_chat_member.user
    # New status
    new_status = event.new_chat_member.status
    
    now = datetime.now(_tz())

    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M:%S")
    full_name = user.full_name or "Без имени"
    username = f"@{user.username}" if user.username else ""
    user_id = str(user.id)

    if new_status == KICKED:
        action = "❌ Отписка (кик)"
        logging.info(f"🚫 User kicked/banned: {full_name} ({user_id})")
    else:
        action = "❌ Отписка"
        logging.info(f"👋 User left: {full_name} ({user_id})")

    # Запись в Google Sheets
    await state.sheet_queue.put([date_str, time_str, user_id, full_name, username, action])

    # Счётчик для периодической сводки
    state.periodic_leaves.increment()

    # Учёт в surge detector (не триггерит алерт)
    if state.surge_detector:
        state.surge_detector.record_leave()
