"""
Подготовка списка опций для КП.

Сам шаблон собирается кодом (kp.py) — здесь только приводим опции в порядок:
выкидываем услуги дилера, сокращаем формулировки, а главное — сортируем по
ценности. В подпись к фото влезает около тридцати строк, поэтому важно, чтобы
под обрезку уходило базовое, а не то, что продаёт машину.

ИИ здесь работает сортировщиком, а не автором. Каждая строка его ответа
сверяется с опциями объявления: если это сокращение исходной строки — берём
формулировку ИИ, если он что-то домыслил — берём текст объявления, а если
строку опознать не удалось, выбрасываем. Обещать клиенту опцию, которой у
машины нет, дороже, чем потерять строчку.
"""
import os
import re

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openai/gpt-4o-mini"

# Услуги дилера — к самой машине отношения не имеют, убираем совсем.
# Парсер их отсекает по разметке, но в старых черновиках и в истории они
# ещё лежат вперемешку с опциями.
_SERVICES = (
    "финансирование", "лизинг", "тест-драйв", "трейд-ин", "trade-in",
    "обмен", "кредит", "доставка", "гарантия производителя", "гарантия мобильности",
    "страховка", "ремонтный", "ремонтная", "окрасочный", "умный ремонт",
    "автостекло", "шиномонтаж", "склад шин", "запасные части", "услуги регистрации",
    "инспекция", "обучающая организация", "прокатных автомобилей",
    "подержанные автомобили", "подготовка транспортных средств",
    "официальным оператором",
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

# Ни одной опции по имени: модель охотно переписывает примеры из промта в
# ответ, и в КП появляется полный привод у заднеприводной машины.
_SYSTEM_PROMPT = f"""Ты готовишь блок «Комплектация» для коммерческого предложения
на автомобиль. Клиент читает его с телефона, места мало.

Тебе дают список опций конкретной машины. Твоя работа — отбор и порядок,
а не сочинение: выбери не больше {AI_TOP_LINES} строк и расставь их по убыванию
ценности для покупателя.

Порядок ценности:
1. то, что редко встречается и дорого стоит при заказе;
2. комфорт, который чувствуется в каждой поездке;
3. системы помощи водителю и безопасность сверх обязательной;
4. мультимедиа и связь.
В конец или за борт — то, что есть на любой машине этого класса и года.

Жёсткие правила:
— бери строки только из присланного списка. Не добавляй ни одной опции от себя,
  даже если она обычна для этой модели: клиент получит машину без неё;
— одна строка ответа — одна строка из списка, не объединяй и не разделяй;
— формулировку можно сократить и перевести на русский, но только теми словами,
  которые уже есть в этой строке. Ничего не дописывай;
— в ответе только строки опций: без нумерации, дефисов, markdown,
  заголовков и пояснений."""


def _is_service(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in _SERVICES)


def _is_low_value(text: str) -> bool:
    low = text.lower()
    return any(b in low for b in _LOW_VALUE)


def _stem(word: str) -> str:
    """Грубая основа слова: «руля» и «руль» → «рул», «сидений» → «сиден»."""
    return word[:5].rstrip("аеёиоуыэюяьъй")


def _stems(text: str) -> set[str]:
    """Основы значимых слов строки: «Подогрев сидений» → {'подог', 'сиден'}."""
    return {_stem(w) for w in re.findall(r"[^\W\d_]{4,}", text.lower(), re.UNICODE)}


def _best_option(stems: set[str], options_low: list[str]) -> tuple[int, int] | None:
    """
    Опция объявления, о которой говорит строка ИИ, и сколько её слов подтвердилось.

    Ищем подстрокой по всему тексту опции, а не по отдельным словам: у mobile.de
    хватает слитных формулировок вроде «Электросиденья», где нужное слово внутри.
    Если больше половины слов строки взято не из этой опции — считаем, что ИИ
    сочинил, и не опознаём её вовсе.
    """
    best_i, best_hit = -1, 0
    for i, low in enumerate(options_low):
        hit = sum(1 for s in stems if s in low)
        if hit > best_hit or (hit == best_hit and hit and len(low) < len(options_low[best_i])):
            best_i, best_hit = i, hit
    if best_hit * 2 < len(stems):
        return None
    return best_i, best_hit


def _apply_order(lines: list[str], options: list[str]) -> list[str]:
    """Переносит порядок, предложенный ИИ, на реальные опции объявления."""
    options_low = [o.lower() for o in options]
    options_stems = [_stems(o) for o in options]

    used: set[int] = set()
    out: list[str] = []

    for line in lines:
        stems = _stems(line)
        if not stems:
            continue
        found = _best_option(stems, options_low)
        if not found:
            continue
        i, hit = found
        if i in used:
            continue
        used.add(i)
        # Все слова строки нашлись в одной опции — это её сокращение, берём как есть.
        # Иначе ИИ что-то добавил от себя, и в КП идёт формулировка объявления.
        out.append(line if hit == len(stems) else options[i])
        # Строка целиком называет ещё одну опцию («Android Auto и Apple CarPlay») —
        # ставим её следом, чтобы не уехала в самый низ списка
        for j, opt_stems in enumerate(options_stems):
            if j not in used and len(opt_stems) >= 2 and opt_stems <= stems:
                used.add(j)
                out.append(options[j])

    if len(out) < 5:                    # ответ невнятный — доверяем своей подготовке
        return options

    # Хвостом дописываем то, что модель не назвала: если места хватит,
    # опции всё равно попадут в КП, а не потеряются
    return out + [o for i, o in enumerate(options) if i not in used]


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
                    "temperature": 0.1,
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

    return _apply_order(lines, prepared)
