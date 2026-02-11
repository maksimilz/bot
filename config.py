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

# --- ОЧЕРЕДЬ ---
MAX_RETRIES = 3  # количество попыток записи в таблицу

# --- OPENROUTER LLM ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "Arcee AI: Trinity Large Preview (free)")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
