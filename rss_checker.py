import asyncio
import logging
import os
import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot

from config import ADMIN_ID, RSS_FEED_URL

# Используем главную страницу для скрапинга, так как RSS возвращает 403
SITE_BASE_URL = "https://77.rospotrebnadzor.ru"
SITE_NEWS_URL = SITE_BASE_URL  # новости — на главной

LAST_NEWS_FILE = "last_news_id.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Referer": "https://www.google.com/",
}


def get_last_news_id():
    if os.path.exists(LAST_NEWS_FILE):
        with open(LAST_NEWS_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return None


def set_last_news_id(news_id):
    with open(LAST_NEWS_FILE, "w", encoding="utf-8") as f:
        f.write(news_id)


async def fetch_latest_news():
    """
    Парсит главную страницу сайта и возвращает список новостей:
    [{"title": "...", "link": "https://..."}, ...]
    Возвращает пустой список при ошибке.
    """
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
            async with session.get(SITE_NEWS_URL, timeout=timeout, allow_redirects=True) as response:
                if response.status != 200:
                    logging.error(f"Site fetch error: {response.status}")
                    return []
                html = await response.text(errors="replace")

        soup = BeautifulSoup(html, "html.parser")

        news = []
        # Ищем все ссылки внутри блока .blog (основной контент Joomla)
        blog_div = soup.find("div", class_="blog")
        if not blog_div:
            # Fallback: ищем ссылки с /index.php/ во всём документе
            blog_div = soup

        for a_tag in blog_div.find_all("a", href=True):
            href = a_tag["href"]
            # Фильтруем только ссылки на статьи (содержат цифровой ID)
            if "/index.php/" not in href:
                continue
            # Пропускаем служебные/меню ссылки (без дефиса-числа в конце)
            import re
            if not re.search(r"-\d{4,}-", href) and not re.search(r"/\d{3,}[^/]*$", href):
                continue

            title = a_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            full_link = href if href.startswith("http") else SITE_BASE_URL + href

            # Дедупликация по ссылке
            if not any(n["link"] == full_link for n in news):
                news.append({"title": title, "link": full_link})

        return news

    except Exception as e:
        logging.error(f"Error fetching news page: {e}")
        return []


async def check_rss_feed(bot: Bot):
    if not ADMIN_ID:
        return

    news_list = await fetch_latest_news()
    if not news_list:
        return

    # Используем ссылку первой новости как уникальный ID
    latest_id = news_list[0]["link"]
    last_id = get_last_news_id()

    if last_id is None:
        # Первый запуск — просто сохраняем, не спамим
        set_last_news_id(latest_id)
        logging.info(f"RSS: первый запуск, сохранён ID: {latest_id}")
        return

    if latest_id == last_id:
        return  # Новостей нет

    # Собираем все новые статьи до последней известной
    new_articles = []
    for item in news_list:
        if item["link"] == last_id:
            break
        new_articles.append(item)

    # Отправляем от старых к новым
    for item in reversed(new_articles):
        text = (
            f"🆕 <b>Новая новость на сайте!</b>\n\n"
            f"<a href='{item['link']}'>{item['title']}</a>"
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                text,
                parse_mode="HTML",
                disable_notification=False,
            )
        except Exception as e:
            logging.error(f"Ошибка отправки новости: {e}")

    set_last_news_id(latest_id)
    logging.info(f"RSS: отправлено {len(new_articles)} новых статей")
