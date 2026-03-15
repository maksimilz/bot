import time
from collections import deque
from dataclasses import dataclass
from enum import Enum


class WaveAction(Enum):
    """Действия, которые нужно предпринять по волне."""
    NONE = "none"
    SEND_UPDATE = "update"      # Промежуточное обновление
    SEND_SUMMARY = "summary"    # Итоговый алерт — волна завершена


@dataclass
class WaveInfo:
    """Данные о текущей/завершённой волне."""
    joins: int
    leaves: int
    duration_seconds: float
    net: int

    @property
    def duration_minutes(self) -> int:
        return max(1, round(self.duration_seconds / 60))


class SurgeDetector:
    """Отслеживает всплески подписок за скользящее окно.
    Учитывает и подписки, и отписки.
    Поддерживает отслеживание волны с итоговым алертом."""

    def __init__(
        self,
        window_seconds: int = 300,
        threshold: int = 10,
        cooldown_seconds: int = 300,
        quiet_period: int = 120,
        update_interval: int = 300,
    ):
        self.window_seconds = window_seconds
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.quiet_period = quiet_period
        self.update_interval = update_interval

        # --- Скользящее окно (для первичного обнаружения) ---
        self._join_timestamps: deque[float] = deque()
        self._leave_timestamps: deque[float] = deque()
        self._last_alert_time: float = 0.0

        # --- Отслеживание волны ---
        self._wave_active: bool = False
        self._wave_joins: int = 0
        self._wave_leaves: int = 0
        self._wave_start_time: float = 0.0
        self._last_event_time: float = 0.0
        self._last_update_time: float = 0.0

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._join_timestamps and self._join_timestamps[0] < cutoff:
            self._join_timestamps.popleft()
        while self._leave_timestamps and self._leave_timestamps[0] < cutoff:
            self._leave_timestamps.popleft()

    def _start_wave(self, now: float, initial_joins: int, initial_leaves: int) -> None:
        """Начинает отслеживание новой волны."""
        self._wave_active = True
        self._wave_joins = initial_joins
        self._wave_leaves = initial_leaves
        if self._join_timestamps:
            self._wave_start_time = self._join_timestamps[0]
        else:
            self._wave_start_time = now
        self._last_event_time = now
        self._last_update_time = now
        
        # Очищаем окно, чтобы эти события не перетекли в следующую волну
        self._join_timestamps.clear()
        self._leave_timestamps.clear()

    def record_join(self) -> tuple[bool, int, int]:
        """Записывает подписку и проверяет порог.
        Возвращает (нужен_ли_алерт, подписок_за_окно, отписок_за_окно)."""
        now = time.monotonic()

        # Если волна активна, просто учитываем в волне
        if self._wave_active:
            self._wave_joins += 1
            self._last_event_time = now
            return False, 0, 0

        self._join_timestamps.append(now)
        self._cleanup(now)
        joins = len(self._join_timestamps)
        leaves = len(self._leave_timestamps)

        # Проверяем порог для первичного surge-алерта
        if joins >= self.threshold and (now - self._last_alert_time) >= self.cooldown_seconds:
            self._last_alert_time = now
            self._start_wave(now, joins, leaves)
            return True, joins, leaves

        return False, joins, leaves

    def record_leave(self) -> None:
        """Записывает отписку (не триггерит surge-алерт)."""
        now = time.monotonic()

        # Учитываем в волне
        if self._wave_active:
            self._wave_leaves += 1
            self._last_event_time = now
            return

        self._leave_timestamps.append(now)
        self._cleanup(now)

    def check_wave(self) -> tuple[WaveAction, WaveInfo | None]:
        """Проверяет статус волны. Вызывать периодически (каждые 30 сек).
        Возвращает (действие, данные_волны)."""
        if not self._wave_active:
            return WaveAction.NONE, None

        now = time.monotonic()
        elapsed_since_event = now - self._last_event_time
        duration = now - self._wave_start_time

        wave_info = WaveInfo(
            joins=self._wave_joins,
            leaves=self._wave_leaves,
            duration_seconds=duration,
            net=self._wave_joins - self._wave_leaves,
        )

        # Волна завершена — тишина дольше quiet_period
        if elapsed_since_event >= self.quiet_period:
            # Используем время последнего события для точной длительности
            wave_info.duration_seconds = self._last_event_time - self._wave_start_time
            self._wave_active = False
            return WaveAction.SEND_SUMMARY, wave_info

        # Промежуточное обновление — волна длится долго
        elapsed_since_update = now - self._last_update_time
        if elapsed_since_update >= self.update_interval:
            self._last_update_time = now
            return WaveAction.SEND_UPDATE, wave_info

        return WaveAction.NONE, None
        
    def is_wave_active(self) -> bool:
        """Возвращает флаг активности волны."""
        return self._wave_active

    def get_counts(self) -> tuple[int, int]:
        """Текущие счётчики за окно: (joins, leaves)."""
        if self._wave_active:
            return self._wave_joins, self._wave_leaves
            
        now = time.monotonic()
        self._cleanup(now)
        return len(self._join_timestamps), len(self._leave_timestamps)

