import time
from collections import deque


class SurgeDetector:
    """Отслеживает всплески подписок за скользящее окно.
    Учитывает и подписки, и отписки."""

    def __init__(
        self,
        window_seconds: int = 300,
        threshold: int = 10,
        cooldown_seconds: int = 300,
    ):
        self.window_seconds = window_seconds
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._join_timestamps: deque[float] = deque()
        self._leave_timestamps: deque[float] = deque()
        self._last_alert_time: float = 0.0

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._join_timestamps and self._join_timestamps[0] < cutoff:
            self._join_timestamps.popleft()
        while self._leave_timestamps and self._leave_timestamps[0] < cutoff:
            self._leave_timestamps.popleft()

    def record_join(self) -> tuple[bool, int, int]:
        """Записывает подписку и проверяет порог.
        Возвращает (нужен_ли_алерт, подписок_за_окно, отписок_за_окно)."""
        now = time.monotonic()
        self._join_timestamps.append(now)
        self._cleanup(now)
        joins = len(self._join_timestamps)
        leaves = len(self._leave_timestamps)

        if joins >= self.threshold and (now - self._last_alert_time) >= self.cooldown_seconds:
            self._last_alert_time = now
            return True, joins, leaves
        return False, joins, leaves

    def record_leave(self) -> None:
        """Записывает отписку (не триггерит surge-алерт)."""
        now = time.monotonic()
        self._leave_timestamps.append(now)
        self._cleanup(now)

    def get_counts(self) -> tuple[int, int]:
        """Текущие счётчики за окно: (joins, leaves)."""
        now = time.monotonic()
        self._cleanup(now)
        return len(self._join_timestamps), len(self._leave_timestamps)
