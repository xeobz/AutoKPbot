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

# Сколько строк просим у модели: в подпись к фото влезает около 30-35,
# остальное всё равно срежет лимит — пусть лучше отберёт лучшее
AI_TOP_LINES = 34

_SYSTEM_PROMPT = f"""Ты продающий копирайтер автодилера. Готовишь блок «Комплектация»
для коммерческого предложения, которое клиент читает с телефона.

Тебе дают полный список опций автомобиля. Отбери и выстрой по убыванию
ценности для покупателя не более {AI_TOP_LINES} строк.

Что ставить наверх (именно это продаёт машину):
— всё редкое и дорогое: пневмоподвеска, полный привод, панорама, матричная
  оптика, премиум-аудио, ТВ, холодильник, VIP-салон, кастом;
— комфорт, который чувствуют сразу: массаж, вентиляция и подогрев сидений,
  подогрев руля и стекла, электрорегулировки, климат по зонам;
— современные ассистенты: адаптивный круиз, удержание в полосе, слепые зоны,
  автопарковка, камеры кругового обзора, проекция на стекло;
— мультимедиа: навигация, CarPlay/Android Auto, беспроводная зарядка.

Что вниз или вовсе убрать, если не хватает места: ABS, ESP, подушки, Isofix,
иммобилайзер, центральный замок, стеклоподъёмники, бортовой компьютер,
радио, USB, датчики дождя и света, запаска, коврики.

Правила вывода:
— только строки опций, по одной в строке, без нумерации, дефисов и markdown;
— на русском, коротко и по-человечески, до 40 символов в строке;
— похожие опции объединяй в одну строку («Подогрев сидений и руля»),
  чтобы освободить место под другие;
— ничего не выдумывай: бери только то, что есть в списке;
— никаких заголовков, пояснений и итогов."""


def _is_service(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in _SERVICES)


def _is_low_value(text: str) -> bool:
    low = text.lower()
    return any(b in low for b in _LOW_VALUE)


def _stem(word: str) -> str:
    """
    Грубая основа слова: «руля» и «руль» → «рул», «сидений» и «сиденья» → «сиден».

    Пять букв — компромисс: короче начинают склеиваться разные слова
    («автопарковка» и «автоматический»), длиннее — расходятся падежи.
    """
    return word[:5].rstrip("аеёиоуыэюяьъй")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE) if len(w) >= 4]


def _keep_only_real(lines: list[str], source: list[str]) -> list[str]:
    """
    Оставляет строки, которые действительно опираются на список опций.

    Модель охотно дописывает «Камеры кругового обзора» или «Полный привод»,
    даже если их нет в объявлении. В КП это обещание клиенту того, чего у
    машины нет, поэтому такие строки выбрасываем.
    """
    pool = {_stem(w) for opt in source for w in _words(opt)}
    kept: list[str] = []
    for line in lines:
        words = _words(line)
        if not words:                      # строка из коротких слов — доверяем
            kept.append(line)
            continue
        if all(_stem(w) in pool for w in words):
            kept.append(line)
    return kept


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
        f"Опции автомобиля ({len(prepared)} шт.). Отбери лучшее для КП:\n\n"
        + "\n".join(prepared)
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

    # Выбрасываем то, чего в объявлении не было
    lines = _keep_only_real(lines, prepared)

    # Ответ невнятный — доверяем своей подготовке
    if len(lines) < 5:
        return prepared

    # Хвостом дописываем то, что модель не назвала: если места хватит,
    # опции всё равно попадут в КП, а не потеряются
    joined = " | ".join(lines).lower()
    rest = [o for o in prepared if o.lower()[:10] not in joined]
    return lines + rest
