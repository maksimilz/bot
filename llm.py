"""
Модуль интеграции с OpenRouter LLM.

Использование:
    from llm import ask_llm

    messages = [
        {"role": "system", "content": "Ты полезный ассистент."},
        {"role": "user", "content": "Привет!"},
    ]
    answer = await ask_llm(messages)

Перед использованием задай переменные окружения:
    OPENROUTER_API_KEY  — API-ключ OpenRouter (обязательно)
    OPENROUTER_MODEL    — модель (по умолчанию arcee-ai/trinity-large-preview:free)
"""
import logging

import aiohttp

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL


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
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_BASE_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
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
