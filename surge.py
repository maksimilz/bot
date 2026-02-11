import time
from collections import deque


class SurgeDetector:
    """Отслеживает всплески подписок за скользящее окно."""

    def __init__(
        self,
        window_seconds: int = 300,
        threshold: int = 10,
        cooldown_seconds: int = 300,
    ):
        self.window_seconds = window_seconds
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._timestamps: deque[float] = deque()
        self._last_alert_time: float = 0.0

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def record_and_check(self) -> tuple[bool, int]:
        """Записывает подписку и проверяет порог.
        Возвращает (нужен_ли_алерт, количество_за_окно)."""
        now = time.monotonic()
        self._timestamps.append(now)
        self._cleanup(now)
        count = len(self._timestamps)

        if count >= self.threshold and (now - self._last_alert_time) >= self.cooldown_seconds:
            self._last_alert_time = now
            return True, count
        return False, count
