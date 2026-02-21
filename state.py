"""
Глобальное runtime-состояние бота.
Инициализируется в main() при запуске.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from surge import SurgeDetector


class AtomicCounter:
    """Потокобезопасный счётчик с атомарным сбросом."""

    def __init__(self) -> None:
        self._value: int = 0

    def increment(self) -> None:
        self._value += 1

    def reset(self) -> int:
        """Атомарно возвращает текущее значение и сбрасывает в 0."""
        val = self._value
        self._value = 0
        return val

    @property
    def value(self) -> int:
        return self._value


# Очередь записи в Google Sheets — создаётся в main()
sheet_queue: asyncio.Queue | None = None

# Детектор всплесков подписок — создаётся в main()
surge_detector: "SurgeDetector | None" = None

# Счётчики для периодической мини-сводки (сбрасываются после отправки)
periodic_joins: AtomicCounter = AtomicCounter()
periodic_leaves: AtomicCounter = AtomicCounter()

# История сообщений для контекста LLM (user_id -> deque)
# Храним последние 10 сообщений
messages_history: dict[int, deque] = {}
