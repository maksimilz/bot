import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import ChatMemberUpdatedFilter, Command, IS_NOT_MEMBER, IS_MEMBER, MEMBER, KICKED
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ChatMemberUpdated,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config import (
    ADMIN_ID,
    SURGE_WINDOW_SECONDS,
    SURGE_THRESHOLD,
    SYSTEM_PROMPT,
)
import config
from sheets import write_daily_row, read_last_rows
from llm import ask_llm
from post_formatter import format_social_post
from web_search import search_web, format_search_context
import state as app_state


router = Router()

# --- ТЕКСТ КНОПОК ---
BTN_STATS = "📊 Статистика"
BTN_ASK = "🤖 Спросить ИИ"
BTN_SEARCH = "🔍 Поиск"
BTN_NEW = "🗑 Новый диалог"
BTN_HELP = "❓ Помощь"
BTN_POST = "📝 Подготовить пост"
BTN_CANCEL = "❌ Отмена"


# --- FSM-состояния ---
class BotStates(StatesGroup):
    waiting_ask = State()      # ждём текст вопроса для ИИ
    waiting_search = State()   # ждём текст запроса для поиска
    waiting_post = State()


def _tz():
    return ZoneInfo("Europe/Moscow")


def main_keyboard() -> ReplyKeyboardMarkup:
    """Основная клавиатура с кнопками."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_ASK)],
            [KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_NEW)],
            [KeyboardButton(text=BTN_POST), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def _cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой отмены (когда ожидаем ввод)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


# --- ЕЖЕДНЕВНАЯ ЗАПИСЬ СТАТИСТИКИ ---
async def reset_daily_stats():
    """Сбрасывает счётчики за день и записывает в таблицу ровно в 00:00."""
    now = datetime.now(_tz())
    # В 00:00 "вчера" — это завершившийся день
    yesterday = (now - timedelta(days=1)).strftime("%d.%m.%Y")
    
    t_joins = app_state.today_joins.reset()
    t_leaves = app_state.today_leaves.reset()
    
    await write_daily_row(yesterday, t_joins, t_leaves)
    app_state.save_counters()
    logging.info(f"✅ Итоги дня за {yesterday} сброшены и сохранены в таблицу.")


# === КОМАНДЫ (остаются для удобства + обрабатываем кнопки) ===

# --- /start: показать клавиатуру ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext = None):
    """Приветствие и показ клавиатуры."""
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.clear()
    await message.answer(
        "👋 Привет! Используй кнопки ниже для управления ботом.",
        reply_markup=main_keyboard(),
    )


# --- КНОПКА «Отмена» (из любого состояния) ---
@router.message(F.text == BTN_CANCEL)
async def btn_cancel(message: Message, state: FSMContext = None):
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.clear()
    await message.answer("↩️ Отменено.", reply_markup=main_keyboard())


# --- СТАТИСТИКА ---
@router.message(F.text == BTN_STATS)
@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot, state: FSMContext = None):
    """Статистика: сегодня из памяти + неделя из таблицы."""
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.clear()

    today = datetime.now(_tz()).strftime("%d.%m.%Y")
    t_joins = app_state.today_joins.value
    t_leaves = app_state.today_leaves.value
    t_net = t_joins - t_leaves
    t_net_str = f"+{t_net}" if t_net >= 0 else str(t_net)

    # Surge
    surge_info = ""
    if app_state.surge_detector:
        if app_state.surge_detector.is_wave_active():
            joins_w, leaves_w = app_state.surge_detector.get_counts()
            surge_info = (
                f"\n🔥 <b>АКТИВНАЯ ВОЛНА:</b> "
                f"<b>+{joins_w} / -{leaves_w}</b>"
            )
        else:
            joins_w, leaves_w = app_state.surge_detector.get_counts()
            if joins_w > 0 or leaves_w > 0:
                surge_info = (
                    f"\n⚡ За последние {SURGE_WINDOW_SECONDS // 60} мин: "
                    f"<b>+{joins_w} / -{leaves_w}</b>"
                )

    # Читаем последние 7 дней из таблицы
    last_rows = await read_last_rows(7)
    w_joins = sum(r["joins"] for r in last_rows)
    w_leaves = sum(r["leaves"] for r in last_rows)
    w_net = w_joins - w_leaves
    w_net_str = f"+{w_net}" if w_net >= 0 else str(w_net)
    last_total = last_rows[-1]["total"] if last_rows else 0

    week_lines = ""
    if last_rows:
        for r in last_rows:
            week_lines += f"  {r['date']}: ➕{r['joins']} ➖{r['leaves']} ({r['net']})\n"

    text = (
        f"📊 <b>Текущая статистика</b>\n\n"
        f"📅 Сегодня ({today}):\n"
        f"   ➕ {t_joins}  ➖ {t_leaves}  (итого {t_net_str})\n"
        f"{surge_info}\n"
        f"📆 <b>Последние 7 дней:</b>\n"
        f"{week_lines}"
        f"   Итого: ➕{w_joins} ➖{w_leaves} ({w_net_str})\n\n"
        f"👥 <b>Подписчиков ~{last_total:,}</b>".replace(",", " ")
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard())


# --- ПОМОЩЬ ---
@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext = None):
    """Список доступных команд для админа."""
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.clear()

    text = (
        "📋 <b>Доступные команды</b>\n\n"
        f"{BTN_STATS} — Текущая статистика подписчиков\n"
        f"{BTN_ASK} — Задать вопрос ИИ (помнит контекст)\n"
        f"{BTN_SEARCH} — Поиск в интернете через ИИ\n"
        f"{BTN_NEW} — Новый диалог (очистить память ИИ)\n"
        f"{BTN_POST} (/post) — Черновик для соцсетей в личном чате\n"
        f"{BTN_HELP} — Этот список команд\n\n"
        "<i>Бот автоматически отслеживает подписки и отписки, "
        "записывает данные в Google Таблицу.</i>\n\n"
        "📬 <b>Уведомления:</b>\n"
        f"⚡ Surge-алерт: ≥{SURGE_THRESHOLD} подписок "
        f"за {SURGE_WINDOW_SECONDS // 60} мин"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard())


# --- НОВЫЙ ДИАЛОГ ---
@router.message(F.text == BTN_NEW)
@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext = None):
    """Очистить память диалога с ИИ."""
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.clear()

    user_id = message.from_user.id
    if user_id in app_state.messages_history:
        app_state.messages_history[user_id].clear()
    await message.answer("🗑 Память очищена. Новый диалог начат!", reply_markup=main_keyboard())


@router.message(F.text == BTN_POST, F.chat.type == ChatType.PRIVATE)
@router.message(Command("post"), F.chat.type == ChatType.PRIVATE)
async def cmd_post(message: Message, state: FSMContext, command: CommandObject | None = None):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return

    source = (command.args or "").strip() if command else ""
    if not source:
        await state.set_state(BotStates.waiting_post)
        await message.answer(
            "Пришли исходный текст одним сообщением. Верну черновик для соцсетей.",
            reply_markup=_cancel_keyboard(),
        )
        return

    await state.clear()
    await _send_social_post(message, source)


async def _send_social_post(message: Message, source: str):
    parts = await format_social_post(source)
    if not parts:
        await message.answer(
            "Не удалось подготовить черновик. Повтори /post и пришли текст ещё раз.",
            reply_markup=main_keyboard(),
        )
        return

    for index, part in enumerate(parts):
        await message.answer(
            part,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=main_keyboard() if index == len(parts) - 1 else None,
        )


# --- СПРОСИТЬ ИИ (кнопка → ожидание ввода) ---
@router.message(F.text == BTN_ASK)
async def btn_ask(message: Message, state: FSMContext = None):
    """Нажатие кнопки «Спросить ИИ» — переходим в режим ожидания вопроса."""
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.set_state(BotStates.waiting_ask)
    await message.answer(
        "✏️ Напиши свой вопрос для ИИ:",
        reply_markup=_cancel_keyboard(),
    )


@router.message(Command("ask"))
async def cmd_ask(message: Message):
    """Команда /ask с текстом сразу."""
    if message.from_user.id != ADMIN_ID:
        return

    question = message.text.replace("/ask", "", 1).strip()
    if not question:
        await message.answer(
            "✏️ Напиши вопрос после /ask\n\nПример: /ask Какая погода в Москве?",
            reply_markup=main_keyboard(),
        )
        return

    await _process_ask(message, question)


@router.message(BotStates.waiting_ask)
async def process_ask_input(message: Message, state: FSMContext = None):
    """Получили текст вопроса после нажатия кнопки «Спросить ИИ»."""
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.clear()
    await _process_ask(message, message.text)


async def _process_ask(message: Message, question: str):
    """Общая логика обработки вопроса для ИИ."""
    user_id = message.from_user.id

    # Инициализация истории для пользователя
    if user_id not in app_state.messages_history:
        app_state.messages_history[user_id] = deque(maxlen=10)

    history = app_state.messages_history[user_id]
    history.append({"role": "user", "content": question})

    await message.answer("🤔 Думаю...")

    # Отправляем всю историю диалога с системным промптом
    system_msg = {"role": "system", "content": SYSTEM_PROMPT}
    answer = await ask_llm(
        messages=[system_msg] + list(history),
    )

    if answer:
        history.append({"role": "assistant", "content": answer})
        await message.answer(answer, reply_markup=main_keyboard())
    else:
        # Если ошибка, удаляем последний вопрос из истории, чтобы не засорять
        if history and history[-1]["role"] == "user":
            history.pop()
        await message.answer(
            f"❌ Не удалось получить ответ от LLM. Проверь баланс/ключ.\nМодель: {config.OPENROUTER_MODEL}",
            reply_markup=main_keyboard(),
        )


# --- ПОИСК (кнопка → ожидание ввода) ---
@router.message(F.text == BTN_SEARCH)
async def btn_search(message: Message, state: FSMContext = None):
    """Нажатие кнопки «Поиск» — переходим в режим ожидания запроса."""
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.set_state(BotStates.waiting_search)
    await message.answer(
        "🔍 Напиши поисковый запрос:",
        reply_markup=_cancel_keyboard(),
    )


@router.message(Command("search"))
async def cmd_search(message: Message):
    """Команда /search с текстом сразу."""
    if message.from_user.id != ADMIN_ID:
        return

    query = message.text.replace("/search", "", 1).strip()
    if not query:
        await message.answer(
            "🔍 Напиши запрос после /search\n\n"
            "Пример: /search последние новости Python",
            reply_markup=main_keyboard(),
        )
        return

    await _process_search(message, query)


@router.message(BotStates.waiting_search)
async def process_search_input(message: Message, state: FSMContext = None):
    """Получили текст запроса после нажатия кнопки «Поиск»."""
    if message.from_user.id != ADMIN_ID:
        return

    if state:
        await state.clear()
    await _process_search(message, message.text)


async def _process_search(message: Message, query: str):
    """Общая логика обработки поискового запроса."""
    await message.answer("🔍 Ищу в интернете...")

    # Поиск в DuckDuckGo
    results = await search_web(query, max_results=5)

    if not results:
        await message.answer(
            "❌ Ничего не найдено. Попробуй другой запрос.",
            reply_markup=main_keyboard(),
        )
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
        await message.answer(answer, reply_markup=main_keyboard())
    else:
        # Если LLM не ответил, покажем сырые результаты
        fallback = "🔍 <b>Результаты поиска:</b>\n\n"
        for r in results[:3]:
            title = r.get("title", "")
            url = r.get("href", "")
            snippet = r.get("body", "")
            fallback += f"📌 <b>{title}</b>\n{snippet}\n{url}\n\n"
        await message.answer(fallback, parse_mode="HTML", reply_markup=main_keyboard())


@router.message(
    BotStates.waiting_post,
    F.chat.type == ChatType.PRIVATE,
    F.text,
    ~F.text.startswith("/"),
)
@router.message(BotStates.waiting_post, F.chat.type == ChatType.PRIVATE, ~F.text)
async def process_post_input(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return

    source = (message.text or "").strip()
    if not source:
        await message.answer("Пришли исходник текстом.", reply_markup=_cancel_keyboard())
        return

    await state.clear()
    await _send_social_post(message, source)


# --- СОБЫТИЯ ПОДПИСКИ / ОТПИСКИ ---
@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> MEMBER))
async def on_user_join(event: ChatMemberUpdated, bot: Bot):
    user = event.new_chat_member.user
    logging.info(f"🔔 Новый подписчик: {user.full_name} ({user.id})")

    # Только счётчики в памяти — в таблицу пишем раз в день агрегатом
    app_state.today_joins.increment()
    app_state.save_counters()

    # Surge detection — алерт только при всплеске
    if app_state.surge_detector:
        is_surge, joins_w, leaves_w = app_state.surge_detector.record_join()
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
    new_status = event.new_chat_member.status
    action_log = "kicked/banned" if new_status == "kicked" else "left"
    logging.info(f"👋 User {action_log}: {user.full_name} ({user.id})")

    # Только счётчики в памяти
    app_state.today_leaves.increment()
    app_state.save_counters()

    # Учёт в surge detector
    if app_state.surge_detector:
        app_state.surge_detector.record_leave()
