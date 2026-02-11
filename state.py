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

# Очередь записи в Google Sheets — создаётся в main()
sheet_queue: asyncio.Queue | None = None

# Детектор всплесков подписок — создаётся в main()
# Детектор всплесков подписок — создаётся в main()
surge_detector: "SurgeDetector | None" = None

# История сообщений для контекста LLM (user_id -> deque)
# Храним последние 10 сообщений
messages_history: dict[int, deque] = {}
