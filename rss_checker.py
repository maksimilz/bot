import asyncio
import logging
import xml.etree.ElementTree as ET
import aiohttp
from aiogram import Bot
import os

from config import ADMIN_ID, RSS_FEED_URL

LAST_NEWS_FILE = "last_news_id.txt"

def get_last_news_id():
    if os.path.exists(LAST_NEWS_FILE):
        with open(LAST_NEWS_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return None

def set_last_news_id(news_id):
    with open(LAST_NEWS_FILE, "w", encoding="utf-8") as f:
        f.write(news_id)

async def check_rss_feed(bot: Bot):
    if not ADMIN_ID or not RSS_FEED_URL:
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(RSS_FEED_URL, timeout=10) as response:
                if response.status != 200:
                    logging.error(f"RSS Fetch Error: {response.status}")
                    return
                content = await response.text()
                
        root = ET.fromstring(content)
        items = root.findall("./channel/item")
        if not items:
            return

        latest_item = items[0]
        guid = latest_item.findtext("guid", default=latest_item.findtext("link"))
        
        last_id = get_last_news_id()
        
        if last_id is None:
            # First run, just save it to avoid spamming existing news
            set_last_news_id(guid)
            return
            
        if guid != last_id:
            new_articles = []
            for item in items:
                item_guid = item.findtext("guid", default=item.findtext("link"))
                if item_guid == last_id:
                    break
                new_articles.append(item)
                
            for item in reversed(new_articles):
                i_title = item.findtext("title", default="Без заголовка")
                i_link = item.findtext("link", default=RSS_FEED_URL)
                text = f"🆕 <b>Новая новость на сайте!</b>\n\n<a href='{i_link}'>{i_title}</a>"
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        text,
                        parse_mode="HTML",
                        disable_notification=False  # Явно включаем звук
                    )
                except Exception as e:
                    logging.error(f"Ошибка отправки RSS алерта: {e}")
            
            set_last_news_id(guid)
            
    except Exception as e:
        logging.error(f"Error checking RSS feed: {e}")
