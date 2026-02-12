"""
Модуль веб-поиска через DuckDuckGo.

Использование:
    from web_search import search_web, format_search_context

    results = await search_web("Python asyncio tutorial")
    context = format_search_context(results)
"""
import asyncio
import logging

from duckduckgo_search import DDGS


async def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Выполняет поиск в DuckDuckGo и возвращает результаты.

    Args:
        query: Поисковый запрос.
        max_results: Максимальное количество результатов.

    Returns:
        Список словарей с ключами: title, href, body.
    """
    try:
        results = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=max_results))
        )
        return results
    except Exception as e:
        logging.error(f"❌ Ошибка поиска DuckDuckGo: {e}")
        return []


def format_search_context(results: list[dict]) -> str:
    """Форматирует результаты поиска в текст для промпта LLM.

    Args:
        results: Результаты из search_web().

    Returns:
        Строка с пронумерованными результатами.
    """
    if not results:
        return "Результатов не найдено."

    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("href", "")
        snippet = r.get("body", "")
        parts.append(f"{i}. {title}\n   {url}\n   {snippet}")

    return "\n\n".join(parts)
