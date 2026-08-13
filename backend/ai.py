"""
ИИ-помощник: отбор и сокращение списка опций для КП.

Сам шаблон КП собирается кодом (kp.py) — ИИ отвечает только за комплектацию,
иначе текст не влезает в лимит подписи Telegram (1024 символа).
"""
import os
import re

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-4o-mini"

# Базовые опции, которые есть у всех и ничего не продают
_BORING = (
    "abs", "esp", "esc", "asr", "isofix", "иммобилайзер", "гидроусилитель",
    "бортовой компьютер", "радио", "подушка безопасности", "подушки безопасности",
    "боковые подушки", "подушка безоп", "центральный замок", "эл. стеклоподъемники",
    "электростеклоподъемники", "сервоусилитель", "power steering", "airbag",
    "start/stop", "старт/стоп", "usb", "aux", "cd", "mp3", "тонированные стекла",
    "нированные стекла", "подстаканник", "подлокотник",
)

_SYSTEM_PROMPT = """Ты помощник автодилера, готовишь коммерческое предложение.
Из списка опций автомобиля отбираешь только те, что реально продают машину.
Правила:
— отвечай на русском, по одной опции в строке, без нумерации, дефисов и markdown;
— формулировки короткие, до 45 символов («Панорамная крыша», «Матричные LED-фары»);
— если опция на другом языке — переведи;
— выбрасывай базовое и скучное: ABS, ESP, подушки, Isofix, иммобилайзер, USB, радио, стеклоподъёмники;
— объединяй родственные опции в одну строку;
— ничего не выдумывай, бери только из списка;
— никаких заголовков и пояснений, только строки опций."""


def _prefilter(features: list[str]) -> list[str]:
    """Убираем дубли и заведомо скучные опции ещё до обращения к ИИ."""
    out: list[str] = []
    seen: set[str] = set()
    for f in features:
        clean = re.sub(r"\s{2,}", " ", (f or "").strip())
        if not clean or len(clean) > 90:
            continue
        low = clean.lower()
        if low in seen:
            continue
        if any(b in low for b in _BORING):
            continue
        seen.add(low)
        out.append(clean)
    return out


async def shorten_options(features: list[str], max_lines: int = 14) -> list[str]:
    """
    Возвращает короткий список опций для КП.
    Если ИИ недоступен — отдаём отфильтрованный исходный список
    (у mobile.de/ru и autoscout24.ru опции и так на русском).
    """
    prefiltered = _prefilter(features)
    if not prefiltered:
        return []

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return prefiltered[:max_lines]

    user_prompt = (
        f"Отбери максимум {max_lines} самых продающих опций из списка:\n\n"
        + "\n".join(prefiltered[:80])
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://localhost",
                    "X-Title": "AutoKP Generator",
                },
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return prefiltered[:max_lines]

    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-—•*0123456789. ").strip()
        line = re.sub(r"\*\*|__", "", line)
        if line and line.lower() not in (l.lower() for l in lines):
            lines.append(line)
    return (lines or prefiltered)[:max_lines]
