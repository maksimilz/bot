import asyncio
import json
import logging
import os

import gspread

from config import SHEET_NAME, MAX_RETRIES

# Глобальный кэш для таблицы
_SHEET_CACHE = None


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


async def process_sheet_queue(sheet_queue: asyncio.Queue):
    """Фоновая задача для пакетной записи в таблицу с ретраями."""
    global _SHEET_CACHE

    while True:
        rows_to_write = []
        try:
            # Ждем первую запись
            first_item = await sheet_queue.get()
            rows_to_write.append(first_item)

            # Собираем остальные доступные записи в течение короткого времени
            try:
                while len(rows_to_write) < 50:
                    item = await asyncio.wait_for(sheet_queue.get(), timeout=2.0)
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
