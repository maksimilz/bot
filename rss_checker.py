import logging
import os
import urllib.parse
import xml.etree.ElementTree as ET
import aiohttp
from aiogram import Bot

from config import ADMIN_ID, RSS_FEED_URL

SITE_BASE_URL = "https://77.rospotrebnadzor.ru"
LAST_NEWS_FILE = "last_news_id.txt"

# Бесплатные HTTP-прокси, которые получают страницу от нашего имени
# (их IP-адреса не входят в стандартные облачные блок-листы)
ALLORIGINS_URL = "https://api.allorigins.win/get?url={}"
CORSPROXY_URL = "https://corsproxy.io/?url={}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
    "Accept": "*/*",
}


def get_last_news_id():
    if os.path.exists(LAST_NEWS_FILE):
        with open(LAST_NEWS_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return None


def set_last_news_id(news_id):
    with open(LAST_NEWS_FILE, "w", encoding="utf-8") as f:
        f.write(news_id)


def _parse_rss_xml(text: str) -> list:
    """Разбирает RSS-XML и возвращает список {'title': ..., 'link': ...}."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        logging.warning(f"XML parse error: {e}")
        return []
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


async def _fetch_rss_via_proxy(session: aiohttp.ClientSession, proxy_template: str, name: str) -> list:
    """Загружает RSS через указанный прокси-сервис."""
    encoded = urllib.parse.quote(RSS_FEED_URL, safe="")
    url = proxy_template.format(encoded)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            if resp.status != 200:
                logging.warning(f"{name}: HTTP {resp.status}")
                return []
            data = await resp.json(content_type=None)
            # allorigins и corsproxy возвращают {'contents': '...xml...'}
            contents = data.get("contents", "")
            if not contents:
                logging.warning(f"{name}: пустой ответ")
                return []
        news = _parse_rss_xml(contents)
        if news:
            logging.info(f"{name}: получено {len(news)} новостей")
        else:
            logging.warning(f"{name}: статьи не найдены в RSS")
        return news
    except Exception as e:
        logging.warning(f"{name} error: {e}")
        return []


async def fetch_latest_news() -> list:
    """
    Получает RSS через бесплатные HTTP-прокси:
    1. allorigins.win  — основной
    2. corsproxy.io    — запасной
    Оба сервиса делают запрос от своего IP, обходя блокировку Render/AWS.
    """
    connector = aiohttp.TCPConnector(ssl=True)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        news = await _fetch_rss_via_proxy(session, ALLORIGINS_URL, "allorigins")
        if news:
            return news
        news = await _fetch_rss_via_proxy(session, CORSPROXY_URL, "corsproxy")
        return news


async def check_rss_feed(bot: Bot):
    if not ADMIN_ID:
        return

    news_list = await fetch_latest_news()
    if not news_list:
        logging.warning("RSS: не удалось получить новости ни одним из методов")
        return

    latest_id = news_list[0]["link"]
    last_id = get_last_news_id()

    if last_id is None:
        set_last_news_id(latest_id)
        logging.info(f"RSS: первый запуск, сохранён ID: {latest_id}")
        return

    if latest_id == last_id:
        return  # Новостей нет

    new_articles = []
    for item in news_list:
        if item["link"] == last_id:
            break
        new_articles.append(item)

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
