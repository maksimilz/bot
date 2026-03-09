"""
Модуль интеграции с OpenRouter LLM.

Использование:
    from llm import ask_llm, close_llm_session

    messages = [
        {"role": "system", "content": "Ты полезный ассистент."},
        {"role": "user", "content": "Привет!"},
    ]
    answer = await ask_llm(messages)

    # При завершении работы:
    await close_llm_session()

Перед использованием задай переменные окружения:
    OPENROUTER_API_KEY  — API-ключ OpenRouter (обязательно)
    OPENROUTER_MODEL    — модель (по умолчанию arcee-ai/trinity-large-preview:free)
"""
import logging

import aiohttp

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL

# Единственная сессия, переиспользуемая между запросами
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    """Возвращает (или создаёт) единственную ClientSession."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
    return _session


async def close_llm_session() -> None:
    """Закрывает HTTP-сессию. Вызвать при graceful shutdown."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


async def ask_llm(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> str | None:
    """
    Отправляет запрос к OpenRouter API и возвращает текст ответа.

    Args:
        messages: История сообщений [{"role": "user", "content": "..."}, ...]
        temperature: Температура генерации (0.0 — детерминированно, 1.0 — креативно).
        max_tokens: Максимальное количество токенов в ответе.

    Returns:
        Текст ответа модели или None при ошибке.
    """
    if not OPENROUTER_API_KEY:
        logging.warning("⚠️ OPENROUTER_API_KEY не задан — LLM запрос пропущен")
        return None

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        session = _get_session()
        async with session.post(
            OPENROUTER_BASE_URL,
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logging.error(
                    f"❌ OpenRouter API ошибка {resp.status}: {error_text}"
                )
                return None

            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    except aiohttp.ClientError as e:
        logging.error(f"❌ Ошибка сети при запросе к OpenRouter: {e}")
        return None
    except (KeyError, IndexError) as e:
        logging.error(f"❌ Неожиданный формат ответа OpenRouter: {e}")
        return None
    except Exception as e:
        logging.error(f"❌ Неизвестная ошибка при запросе к LLM: {e}")
        return None
