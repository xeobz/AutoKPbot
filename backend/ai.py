"""
Подготовка списка опций для КП.

Сам шаблон собирается кодом (kp.py) — здесь только приводим опции в порядок:
выкидываем услуги дилера, переводим и сокращаем формулировки, а главное —
сортируем по ценности. В подпись к фото влезает около сорока строк, поэтому
важно, чтобы под обрезку уходило базовое, а не то, что продаёт машину.
"""
import os
import re

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-4o-mini"

# Услуги дилера — к самой машине отношения не имеют, убираем совсем
_SERVICES = (
    "финансирование", "лизинг", "тест-драйв", "трейд-ин", "trade-in",
    "обмен", "кредит", "доставка", "гарантия производителя",
)

# Базовое, что есть у всех: не выкидываем, но опускаем в конец списка —
# если текст не влезает, режется именно это
_LOW_VALUE = (
    "abs", "esp", "esc", "asr", "isofix", "иммобилайзер", "гидроусилитель",
    "усилитель руля", "подушка безоп", "подушки безоп", "airbag",
    "центральный замок", "стеклоподъемники", "электростекла", "бортовой компьютер",
    "тюнер", "радио", "аудиосистема", "cd-плеер", "usb", "aux", "mp3",
    "противобуксовочная", "система «старт-стоп»", "старт-стоп", "start/stop",
    "подлокотник", "подстаканник", "тонированные стекла", "нированные стекла",
    "летние шины", "зимние шины", "контроль давления в шинах",
    "аварийный комплект", "омыватель фар", "противотуманная",
    "датчик освещенности", "ручное переключение передач",
)

_SYSTEM_PROMPT = """Ты помощник автодилера, готовишь список опций для коммерческого предложения.
Тебе дают список опций автомобиля. Верни ВЕСЬ список, ничего не выбрасывая.
Правила:
— отвечай на русском, по одной опции в строке, без нумерации, дефисов и markdown;
— формулировки короткие, до 40 символов («Панорамная крыша», «Матричные LED-фары»);
— если опция на другом языке — переведи;
— порядок важен: сначала то, что реально продаёт машину (оптика, подвеска,
  салон, мультимедиа, ассистенты), в конце — базовое, что есть у всех;
— ничего не выдумывай и не объединяй, число строк должно совпасть с исходным;
— никаких заголовков и пояснений, только строки опций."""


def _is_service(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in _SERVICES)


def _is_low_value(text: str) -> bool:
    low = text.lower()
    return any(b in low for b in _LOW_VALUE)


def _prepare(features: list[str]) -> list[str]:
    """Чистим дубли и услуги дилера, базовое опускаем в конец."""
    seen: set[str] = set()
    valuable: list[str] = []
    basic: list[str] = []

    for f in features:
        clean = re.sub(r"\s{2,}", " ", (f or "").strip())
        if not clean or len(clean) > 90:
            continue
        low = clean.lower()
        if low in seen or _is_service(clean):
            continue
        seen.add(low)
        (basic if _is_low_value(clean) else valuable).append(clean)

    return valuable + basic


async def shorten_options(features: list[str], max_lines: int | None = None) -> list[str]:
    """
    Список опций для КП: все, что есть у машины, в порядке ценности.
    Сколько из них поместится — решает лимит подписи при сборке текста.

    Если ИИ недоступен или ответил невнятно, отдаём подготовленный список
    как есть: у mobile.de/ru и autoscout24.ru опции и так по-русски.
    """
    prepared = _prepare(features)
    if not prepared:
        return []
    if max_lines:
        prepared = prepared[:max_lines]

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return prepared

    user_prompt = (
        f"Вот {len(prepared)} опций автомобиля. Верни их все, "
        f"по одной в строке, самое ценное сверху:\n\n" + "\n".join(prepared)
    )

    try:
        async with httpx.AsyncClient(timeout=90) as client:
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
        return prepared

    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-—•*0123456789. ").strip()
        line = re.sub(r"\*\*|__", "", line)
        if line and line.lower() not in (l.lower() for l in lines):
            lines.append(line)

    # Модель могла проглотить часть списка — тогда доверяем своей подготовке
    if len(lines) < len(prepared) * 0.8:
        return prepared
    return lines
