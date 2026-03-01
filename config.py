import os
import logging

# --- TELEGRAM ---
TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except (ValueError, TypeError):
    ADMIN_ID = 0

# --- GOOGLE SHEETS ---
SHEET_NAME = "График подписчиков"

# --- ЕЖЕДНЕВНАЯ СВОДКА (по МСК) ---
DAILY_REPORT_HOUR = 9
DAILY_REPORT_MINUTE = 0

# --- SURGE DETECTION ---
SURGE_WINDOW_SECONDS = 300    # 5 минут
SURGE_THRESHOLD = 10          # порог подписчиков
SURGE_COOLDOWN_SECONDS = 300  # не спамить алертами чаще чем раз в 5 мин
SURGE_QUIET_PERIOD = 120      # 2 мин тишины → волна завершена
SURGE_UPDATE_INTERVAL = 300   # промежуточные обновления каждые 5 мин

# --- ПЕРИОДИЧЕСКАЯ МИНИ-СВОДКА ---
PERIODIC_REPORT_HOURS = 3  # интервал мини-сводки в часах

# --- RSS MONITORING ---
RSS_FEED_URL = "https://77.rospotrebnadzor.ru/index.php?format=feed&type=rss"
RSS_CHECK_INTERVAL_MINUTES = 10

# --- ОЧЕРЕДЬ ---
MAX_RETRIES = 3  # количество попыток записи в таблицу

# --- OPENROUTER LLM ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "arcee-ai/trinity-large-preview:free")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

# Системный промпт — «роль» бота (можно менять под свои задачи)
SYSTEM_PROMPT = (
    "Ты — Элантира, высшая эльфийка и хранительница Королевской Библиотеки, живущая уже 3000 лет. "
    "Ты обладаешь огромными знаниями о магии, истории и мире. "
    "Ты вынуждена отвечать на вопросы смертного (пользователя), что тебя слегка утомляет. "
    "Отвечай развернуто, используя элегантный и немного старомодный слог. "
    "Проявляй легкое, едва заметное высокомерие, подчеркивая свой возраст и опыт, но при этом давай максимально точную и полезную информацию."
)
