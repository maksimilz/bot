import asyncio
import json
import logging
import os
from pathlib import Path

import gspread

from config import SHEET_NAME, MAX_RETRIES

# Глобальный кэш для таблицы
_SHEET_CACHE = None

# Файл для сохранения неудачных записей (dead-letter queue)
DEAD_LETTER_FILE = Path(__file__).parent / "failed_rows.jsonl"


def get_sheet(force_refresh=False):
    """Подключение к Google Таблице с кэшированием."""
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


# --- Dead-letter queue ---

def _save_to_dead_letter(rows: list[list]) -> None:
    """Сохраняет неудачные строки в JSONL-файл (append)."""
    try:
        with open(DEAD_LETTER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rows, ensure_ascii=False) + "\n")
        logging.warning(
            f"💾 Сохранено {len(rows)} строк в dead-letter: {DEAD_LETTER_FILE}"
        )
    except Exception as e:
        logging.critical(
            f"🔴 НЕ УДАЛОСЬ СОХРАНИТЬ в dead-letter! Данные потеряны: {e}\n"
            f"Строки: {rows}"
        )


async def _replay_dead_letter() -> None:
    """Переотправляет сохранённые строки из dead-letter файла при старте."""
    if not DEAD_LETTER_FILE.exists():
        return

    logging.info("📂 Найден dead-letter файл, пытаемся переотправить...")
    batches: list[list[list]] = []

    try:
        with open(DEAD_LETTER_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    batches.append(json.loads(line))
    except Exception as e:
        logging.error(f"❌ Ошибка чтения dead-letter файла: {e}")
        return

    if not batches:
        DEAD_LETTER_FILE.unlink(missing_ok=True)
        return

    worksheet = get_sheet(force_refresh=True)
    if not worksheet:
        logging.error("❌ Таблица недоступна, dead-letter остаётся на диске")
        return

    failed_batches: list[list[list]] = []
    replayed = 0

    for batch in batches:
        try:
            await asyncio.to_thread(worksheet.append_rows, batch)
            replayed += len(batch)
        except Exception as e:
            logging.error(f"❌ Не удалось переотправить пачку ({len(batch)} строк): {e}")
            failed_batches.append(batch)

    # Перезаписываем файл: только то, что не удалось
    if failed_batches:
        with open(DEAD_LETTER_FILE, "w", encoding="utf-8") as f:
            for batch in failed_batches:
                f.write(json.dumps(batch, ensure_ascii=False) + "\n")
        logging.warning(
            f"⚠️ Переотправлено {replayed} строк, "
            f"осталось {sum(len(b) for b in failed_batches)} в dead-letter"
        )
    else:
        DEAD_LETTER_FILE.unlink(missing_ok=True)
        logging.info(f"✅ Все {replayed} строк из dead-letter успешно переотправлены")


# --- Основной worker ---

async def process_sheet_queue(sheet_queue: asyncio.Queue):
    """Фоновая задача для пакетной записи в таблицу с ретраями.

    Поддерживает graceful shutdown: при получении None (sentinel)
    дописывает остаток очереди и завершается.
    """
    global _SHEET_CACHE

    # При старте пробуем переотправить dead-letter
    await _replay_dead_letter()

    while True:
        rows_to_write: list[list] = []
        try:
            # Ждем первую запись
            first_item = await sheet_queue.get()

            # Sentinel — сигнал завершения
            if first_item is None:
                # Дренажируем оставшееся в очереди
                while not sheet_queue.empty():
                    item = sheet_queue.get_nowait()
                    if item is not None:
                        rows_to_write.append(item)
                if rows_to_write:
                    await _write_rows(rows_to_write)
                logging.info("🛑 Worker очереди завершён (graceful shutdown)")
                return

            rows_to_write.append(first_item)

            # Собираем остальные доступные записи в течение короткого времени
            try:
                while len(rows_to_write) < 50:
                    item = await asyncio.wait_for(sheet_queue.get(), timeout=2.0)
                    if item is None:
                        # Sentinel пришёл во время сбора — записываем что есть и выходим
                        await _write_rows(rows_to_write)
                        logging.info("🛑 Worker очереди завершён (graceful shutdown)")
                        return
                    rows_to_write.append(item)
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                pass

            # Пишем собранную пачку
            if rows_to_write:
                await _write_rows(rows_to_write)

        except Exception as e:
            logging.error(f"❌ Критическая ошибка в worker очереди: {e}")
            # Сохраняем что успели собрать
            if rows_to_write:
                _save_to_dead_letter(rows_to_write)
            await asyncio.sleep(5)


async def _write_rows(rows: list[list]) -> None:
    """Записывает строки в таблицу с ретраями. При неудаче — в dead-letter."""
    global _SHEET_CACHE

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
            await asyncio.to_thread(worksheet.append_rows, rows)
            logging.info(f"✅ Записано пачкой: {len(rows)} строк")
            return  # Успех
        except gspread.exceptions.APIError as e:
            logging.error(f"❌ Попытка {attempt}/{MAX_RETRIES} — API ошибка: {e}")
            _SHEET_CACHE = None
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logging.error(f"❌ Попытка {attempt}/{MAX_RETRIES} — ошибка: {e}")
            await asyncio.sleep(2 ** attempt)

    # Все попытки исчерпаны — в dead-letter
    _save_to_dead_letter(rows)

