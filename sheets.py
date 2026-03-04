import asyncio
import json
import logging
import os

import gspread

from config import MAX_RETRIES

STATS_SHEET_NAME = "Статистика"
OLD_SHEET_NAME = "График подписчиков"
HEADER = ["Дата", "Подписки", "Отписки", "Чистый прирост", "Всего (накопленно)"]

_gc_cache = None       # gspread client
_stats_sheet = None    # лист «Статистика»


def _get_client():
    global _gc_cache
    if _gc_cache:
        return _gc_cache
    creds_json = os.environ.get("G_SHEETS_KEY")
    if not creds_json:
        logging.error("❌ Нет ключа G_SHEETS_KEY в переменных окружения")
        return None
    try:
        creds_dict = json.loads(creds_json)
        _gc_cache = gspread.service_account_from_dict(creds_dict)
        return _gc_cache
    except Exception as e:
        logging.error(f"❌ Ошибка авторизации Google: {e}")
        return None


def _calc_seed_from_old_sheet(spreadsheet) -> int:
    """Считает начальный total из старого листа событий (joins - leaves)."""
    try:
        old_ws = spreadsheet.sheet1
        rows = old_ws.get_all_values()
        if not rows:
            return 0
        data = rows[1:]  # пропускаем заголовок
        joins = sum(1 for r in data if len(r) >= 6 and "Подписка" in r[5])
        leaves = sum(1 for r in data if len(r) >= 6 and "Отписка" in r[5])
        seed = joins - leaves
        logging.info(f"📊 Seed из старого листа: {joins} подписок - {leaves} отписок = {seed}")
        return seed
    except Exception as e:
        logging.warning(f"Не удалось посчитать seed: {e}")
        return 0


def get_sheet(force_refresh=False):
    """Совместимость со старым кодом — возвращает лист «Статистика»."""
    return get_stats_sheet(force_refresh)


def get_stats_sheet(force_refresh=False):
    """Возвращает лист «Статистика», создаёт если не существует."""
    global _stats_sheet
    if _stats_sheet and not force_refresh:
        return _stats_sheet

    gc = _get_client()
    if not gc:
        return None

    try:
        sh = gc.open(OLD_SHEET_NAME)
    except gspread.exceptions.SpreadsheetNotFound:
        logging.error(f"❌ Таблица '{OLD_SHEET_NAME}' не найдена")
        return None
    except Exception as e:
        logging.error(f"❌ Ошибка открытия таблицы: {e}")
        return None

    # Создаём лист «Статистика» если не существует
    try:
        ws = sh.worksheet(STATS_SHEET_NAME)
        logging.info(f"✅ Лист '{STATS_SHEET_NAME}' найден")
        
        # Если лист пустой (только заголовок), добавим seed-строку
        rows = ws.get_all_values()
        if len(rows) <= 1:
            seed = _calc_seed_from_old_sheet(sh)
            if seed > 0:
                ws.append_row(["(архив до перехода)", 0, 0, 0, seed])
                logging.info(f"✅ Добавлена seed-строка в пустой лист: {seed}")
                
    except gspread.exceptions.WorksheetNotFound:
        logging.info(f"📋 Создаём лист '{STATS_SHEET_NAME}'...")
        ws = sh.add_worksheet(title=STATS_SHEET_NAME, rows=1000, cols=5)
        ws.append_row(HEADER)
        # Записываем seed-строку (итог из старых данных, без даты — только total)
        seed = _calc_seed_from_old_sheet(sh)
        if seed > 0:
            ws.append_row(["(архив до перехода)", 0, 0, 0, seed])
        logging.info(f"✅ Лист '{STATS_SHEET_NAME}' создан, seed = {seed}")

    _stats_sheet = ws
    return ws


async def get_last_total() -> int:
    """Читает последнее значение 'Всего (накопленно)' из таблицы."""
    ws = await asyncio.to_thread(get_stats_sheet)
    if not ws:
        return 0
    try:
        all_rows = await asyncio.to_thread(ws.get_all_values)
        data = all_rows[1:]  # пропускаем заголовок
        for row in reversed(data):
            if len(row) >= 5 and row[4]:
                try:
                    return int(str(row[4]).replace(" ", "").replace(",", ""))
                except ValueError:
                    continue
        return 0
    except Exception as e:
        logging.error(f"Ошибка чтения total: {e}")
        return 0


async def write_daily_row(date_str: str, joins: int, leaves: int) -> bool:
    """
    Добавляет или обновляет строку за указанную дату.
    Возвращает True при успехе.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        ws = await asyncio.to_thread(get_stats_sheet)
        if not ws:
            ws = await asyncio.to_thread(get_stats_sheet, True)
            if not ws:
                await asyncio.sleep(2 ** attempt)
                continue
        try:
            all_rows = await asyncio.to_thread(ws.get_all_values)
            data = all_rows[1:]  # без заголовка

            # Находим предыдущий total
            prev_total = 0
            for row in reversed(data):
                if len(row) >= 5 and row[4]:
                    try:
                        prev_total = int(str(row[4]).replace(" ", "").replace(",", ""))
                        break
                    except ValueError:
                        continue

            net = joins - leaves
            total = prev_total + net

            # Ищем существующую строку за эту дату
            row_index = None
            for i, row in enumerate(data):
                if row and row[0] == date_str:
                    row_index = i + 2  # +1 заголовок, +1 1-based
                    break

            new_row = [date_str, joins, leaves, f"+{net}" if net >= 0 else str(net), total]

            if row_index:
                # Обновляем существующую строку
                await asyncio.to_thread(
                    ws.update,
                    f"A{row_index}:E{row_index}",
                    [new_row]
                )
                logging.info(f"✅ Обновлена строка за {date_str}: +{joins}/-{leaves}, итого={total}")
            else:
                # Новая строка
                await asyncio.to_thread(ws.append_row, new_row)
                logging.info(f"✅ Добавлена строка за {date_str}: +{joins}/-{leaves}, итого={total}")
            return True

        except gspread.exceptions.APIError as e:
            logging.error(f"❌ API ошибка (попытка {attempt}): {e}")
            global _stats_sheet
            _stats_sheet = None
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logging.error(f"❌ Ошибка записи (попытка {attempt}): {e}")
            await asyncio.sleep(2 ** attempt)

    logging.error(f"❌ Не удалось записать строку за {date_str} после {MAX_RETRIES} попыток")
    return False


async def read_last_rows(n: int = 7) -> list[dict]:
    """
    Читает последние N строк из листа «Статистика».
    Возвращает список словарей: {'date', 'joins', 'leaves', 'net', 'total'}.
    """
    ws = await asyncio.to_thread(get_stats_sheet)
    if not ws:
        return []
    try:
        all_rows = await asyncio.to_thread(ws.get_all_values)
        data = all_rows[1:]  # без заголовка
        data = [r for r in data if r and r[0] and r[0] != "(архив до перехода)"]
        recent = data[-n:] if len(data) >= n else data
        result = []
        for row in recent:
            try:
                result.append({
                    "date": row[0] if len(row) > 0 else "",
                    "joins": int(row[1]) if len(row) > 1 and row[1] else 0,
                    "leaves": int(row[2]) if len(row) > 2 and row[2] else 0,
                    "net": row[3] if len(row) > 3 else "0",
                    "total": int(str(row[4]).replace(" ", "").replace(",", "")) if len(row) > 4 and row[4] else 0,
                })
            except (ValueError, IndexError):
                continue
        return result
    except Exception as e:
        logging.error(f"Ошибка чтения последних строк: {e}")
        return []
