"""
Глобальное runtime-состояние бота.
Инициализируется в main() при запуске.

Счётчики подписок/отписок автоматически сохраняются в файл,
чтобы не терять данные при перезапуске.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from surge import SurgeDetector

# Путь к файлу с сохранёнными счётчиками
_PERSIST_FILE = os.path.join(os.path.dirname(__file__), ".counters.json")


class AtomicCounter:
    """Потокобезопасный счётчик с атомарным сбросом."""

    def __init__(self, initial: int = 0) -> None:
        self._value: int = initial
        self._lock = threading.Lock()

    def increment(self) -> None:
        with self._lock:
            self._value += 1

    def reset(self) -> int:
        """Атомарно возвращает текущее значение и сбрасывает в 0."""
        with self._lock:
            val = self._value
            self._value = 0
            return val

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    @value.setter
    def value(self, v: int) -> None:
        with self._lock:
            self._value = v


# Детектор всплесков подписок — создаётся в main()
surge_detector: "SurgeDetector | None" = None

# Счётчики за ТЕКУЩИЙ ДЕНЬ (сбрасываются в полночь при send_daily_report)
today_joins: AtomicCounter = AtomicCounter()
today_leaves: AtomicCounter = AtomicCounter()

# Счётчики для периодической мини-сводки (сбрасываются после каждой мини-сводки)
periodic_joins: AtomicCounter = AtomicCounter()
periodic_leaves: AtomicCounter = AtomicCounter()

# История сообщений для контекста LLM (user_id -> deque)
messages_history: dict[int, deque] = {}


def save_counters() -> None:
    """Сохраняет текущие счётчики в файл."""
    data = {
        "today_joins": today_joins.value,
        "today_leaves": today_leaves.value,
        "periodic_joins": periodic_joins.value,
        "periodic_leaves": periodic_leaves.value,
    }
    try:
        with open(_PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"Ошибка сохранения счётчиков: {e}")


def load_counters() -> None:
    """Загружает счётчики из файла (при старте бота)."""
    if not os.path.exists(_PERSIST_FILE):
        return
    try:
        with open(_PERSIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        today_joins.value = data.get("today_joins", 0)
        today_leaves.value = data.get("today_leaves", 0)
        periodic_joins.value = data.get("periodic_joins", 0)
        periodic_leaves.value = data.get("periodic_leaves", 0)
        logging.info(
            f"📂 Счётчики восстановлены: сегодня +{today_joins.value}/-{today_leaves.value}, "
            f"периодические +{periodic_joins.value}/-{periodic_leaves.value}"
        )
    except Exception as e:
        logging.warning(f"Не удалось загрузить счётчики: {e}")
