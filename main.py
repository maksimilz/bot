import os
import json
import asyncio
from dataclasses import dataclass
from datetime import datetime, date, time
from zoneinfo import ZoneInfo

from aiohttp import web

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message
from aiogram.enums import ChatMemberStatus


DATA_FILE = "events.json"
EVENTS_LOCK = asyncio.Lock()

router = Router()


@dataclass
class JoinEvent:
    ts_iso: str
    user_id: int
    username: str | None
    full_name: str

    def to_dict(self) -> dict:
        return {
            "ts_iso": self.ts_iso,
            "user_id": self.user_id,
            "username": self.username,
            "full_name": self.full_name,
        }


events: list[dict] = []


def _tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("TZ", "Europe/Moscow"))


def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%d.%m"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%d.%m":
                dt = dt.replace(year=datetime.now(_tz()).year)
            return dt.date()
        except ValueError:
            pass
    raise ValueError("Неверный формат даты. Используйте ДД.ММ или ДД.ММ.ГГГГ")


async def load_events() -> None:
    global events
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            events = json.load(f)
        if not isinstance(events, list):
            events = []
    except FileNotFoundError:
        events = []
    except Exception:
        events = []


async def save_events() -> None:
    async with EVENTS_LOCK:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)


def is_join(old_status: ChatMemberStatus, new_status: ChatMemberStatus) -> bool:
    return (old_status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}) and (
        new_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}
    )


@router.chat_member()
async def on_chat_member_updated(update: ChatMemberUpdated, bot: Bot) -> None:
    target_chat_id = int(os.environ["TARGET_CHAT_ID"])
    admin_id = int(os.environ["ADMIN_ID"])

    if update.chat.id != target_chat_id:
        return

    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status

    if not is_join(old_status, new_status):
        return  # игнорируем отписки/прочие смены статуса

    u = update.new_chat_member.user
    full_name = (u.full_name or "").strip() or "Без имени"
    username = u.username

    ts = datetime.now(_tz())
    ev = JoinEvent(
        ts_iso=ts.isoformat(),
        user_id=u.id,
        username=username,
        full_name=full_name,
    )

    events.append(ev.to_dict())
    try:
        await save_events()
    except Exception:
        # JSON — лишь кэш; основной «архив» — сообщение админу
        pass

    uname = f"@{username}" if username else "без username"
    text = (
        "🔔 Новый подписчик!\n"
        f"👤 Имя: {full_name} ({uname})\n"
        f"🆔 ID: {u.id}\n"
        f"📅 Дата: {ts.strftime('%Y-%m-%d %H:%M:%S')} MSK\n"
        "Это сообщение сохранено для отчета."
    )
    await bot.send_message(admin_id, text)


@router.message(Command("report"))
async def report(message: Message) -> None:
    # формат: /report 12.01 18.01  (или с годом)
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Формат: /report ДД.ММ ДД.ММ  (или ДД.ММ.ГГГГ)")
        return

    try:
        d1 = parse_date(parts[1])
        d2 = parse_date(parts[2])
    except ValueError as e:
        await message.answer(str(e))
        return

    start = datetime.combine(d1, time.min, tzinfo=_tz())
    end = datetime.combine(d2, time.max, tzinfo=_tz())

    cnt = 0
    for r in events:
        try:
            ts = datetime.fromisoformat(r["ts_iso"])
        except Exception:
            continue
        if start <= ts <= end:
            cnt += 1

    await message.answer(
        f"Новых вступлений за период {d1.strftime('%d.%m.%Y')}–{d2.strftime('%d.%m.%Y')}: {cnt}"
    )


async def start_web_server() -> None:
    app = web.Application()

    async def handle_root(_: web.Request) -> web.Response:
        return web.Response(text="I am alive")

    app.router.add_get("/", handle_root)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    # держим веб-сервер живым
    await asyncio.Event().wait()


async def main() -> None:
    token = os.environ["BOT_TOKEN"]
    admin_id = int(os.environ["ADMIN_ID"])

    await load_events()

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.send_message(admin_id, "Бот запущен и слушает вступления (chat_member).")

    await asyncio.gather(
        start_web_server(),
        dp.start_polling(
            bot,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
