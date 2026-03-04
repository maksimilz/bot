import asyncio
import logging
import os
import urllib.parse
import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot

from config import ADMIN_ID, RSS_FEED_URL

SITE_BASE_URL = "https://77.rospotrebnadzor.ru"
SITE_NEWS_URL = SITE_BASE_URL

LAST_NEWS_FILE = "last_news_id.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
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


async def fetch_via_rss2json(session: aiohttp.ClientSession) -> list:
    """
    Получает RSS через rss2json.com — публичный прокси, который
    сам запрашивает RSS и возвращает JSON. Обходит блокировку по IP.
    """
    api_url = "https://api.rss2json.com/v1/api.json"
    params = {
        "rss_url": RSS_FEED_URL,
        "api_key": "",   # бесплатно без ключа, но лимит ~10 req/10min
        "count": 20,
    }
    try:
        async with session.get(api_url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
            if data.get("status") != "ok":
                logging.warning(f"rss2json status: {data.get('status')} | {data.get('message','')}")
                return []
            items = data.get("items", [])
            news = []
            for item in items:
                title = item.get("title", "").strip()
                link = item.get("link", "").strip()
                if title and link:
                    news.append({"title": title, "link": link})
            return news
    except Exception as e:
        logging.warning(f"rss2json error: {e}")
        return []


async def fetch_via_direct_rss(session: aiohttp.ClientSession) -> list:
    """Пробует получить RSS напрямую."""
    import xml.etree.ElementTree as ET
    try:
        async with session.get(RSS_FEED_URL, timeout=aiohttp.ClientTimeout(total=20), ssl=False) as resp:
            if resp.status != 200:
                logging.warning(f"Direct RSS status: {resp.status}")
                return []
            text = await resp.text(errors="replace")
        root = ET.fromstring(text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        channel = root.find("channel")
        if channel is None:
            return []
        news = []
        for item in channel.findall("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            title = (title_el.text or "").strip() if title_el is not None else ""
            link = (link_el.text or "").strip() if link_el is not None else ""
            if title and link:
                news.append({"title": title, "link": link})
        return news
    except Exception as e:
        logging.warning(f"Direct RSS error: {e}")
        return []


async def fetch_via_scraping(session: aiohttp.ClientSession) -> list:
    """Парсит HTML главной страницы как запасной вариант."""
    import re
    try:
        async with session.get(SITE_NEWS_URL, timeout=aiohttp.ClientTimeout(total=30), ssl=False) as resp:
            if resp.status != 200:
                logging.error(f"Site fetch error: {resp.status}")
                return []
            html = await resp.text(errors="replace")

        soup = BeautifulSoup(html, "html.parser")
        blog_div = soup.find("div", class_="blog") or soup
        news = []
        for a_tag in blog_div.find_all("a", href=True):
            href = a_tag["href"]
            if "/index.php/" not in href:
                continue
            if not re.search(r"-\d{4,}-", href) and not re.search(r"/\d{3,}[^/]*$", href):
                continue
            title = a_tag.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            full_link = href if href.startswith("http") else SITE_BASE_URL + href
            if not any(n["link"] == full_link for n in news):
                news.append({"title": title, "link": full_link})
        return news
    except Exception as e:
        logging.error(f"Scraping error: {e}")
        return []


async def fetch_latest_news() -> list:
    """
    Пытается получить новости тремя способами:
    1. rss2json.com (прокси, обходит блокировку)
    2. Прямой RSS
    3. HTML-скрапинг главной страницы
    """
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        # Стратегия 1: rss2json
        news = await fetch_via_rss2json(session)
        if news:
            logging.info(f"RSS: получено {len(news)} новостей через rss2json")
            return news

        # Стратегия 2: прямой RSS
        news = await fetch_via_direct_rss(session)
        if news:
            logging.info(f"RSS: получено {len(news)} новостей напрямую")
            return news

        # Стратегия 3: скрапинг HTML
        news = await fetch_via_scraping(session)
        if news:
            logging.info(f"RSS: получено {len(news)} новостей через скрапинг")
        return news



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
