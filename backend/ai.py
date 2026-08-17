"""
Сборка блока «Комплектация» для КП.

Источников два: список оборудования (чек-лист сайта) и описание продавца —
свободный текст на языке объявления, где у дилеров лежит заводская
комплектация с кодами опций. Второй источник заметно богаче: обивка, потолок,
пакеты, диски конкретной модели есть только там.

ИИ переводит и приводит формулировки в порядок, но не выдумывает: вместе
с каждой строкой он обязан вернуть фрагмент объявления, из которого она
взята. Фрагмент проверяется по тексту объявления, и строки без подтверждения
выбрасываются. Обещать клиенту опцию, которой у машины нет, дороже, чем
потерять строчку.
"""
import asyncio
import logging
import os
import re

import httpx

log = logging.getLogger("autokp.ai")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Модель должна читать описание продавца на немецком и переводить его точно.
# gpt-4o-mini и gpt-4.1-mini описание попросту игнорируют — проверено на живых
# объявлениях. Сменить можно секретом AI_MODEL, не трогая код.
MODEL = os.getenv("AI_MODEL") or "google/gemini-2.5-flash"

# Маркер списка в начале строки: «- », «* », «1. ». Цифры в начале названия
# («19" диски», «2-зонный климат») трогать нельзя, поэтому не strip по символам.
_MARKER_RE = re.compile(r"^\s*(?:[-—–•*]+|\d{1,2}[.)])\s+")
# Заводской код опции в скобках: (654), (2TE), (43R) — в КП он не нужен
_CODE_RE = re.compile(r"\s*\((?=[^()]*\d)[A-Za-z0-9\-]{2,6}\)")

# Разделитель между переводом и подтверждающим фрагментом в ответе модели
SEP = "||"

# Услуги дилера — к самой машине отношения не имеют, убираем совсем.
# Парсер mobile.de отсекает их по разметке, но в старых черновиках
# и в истории они ещё лежат вперемешку с опциями.
_SERVICES = (
    "финансирование", "лизинг", "тест-драйв", "трейд-ин", "trade-in",
    "обмен", "кредит", "доставка", "гарантия производителя", "гарантия мобильности",
    "страховка", "ремонтный", "ремонтная", "окрасочный", "умный ремонт",
    "автостекло", "шиномонтаж", "склад шин", "запасные части", "услуги регистрации",
    "инспекция", "обучающая организация", "прокатных автомобилей",
    "подержанные автомобили", "подготовка транспортных средств",
    "официальным оператором",
)

# Базовое, что есть у всех: в запасном списке (когда ИИ недоступен)
# опускаем это в конец, чтобы под обрезку уходило именно оно
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

_SYSTEM_PROMPT = f"""Ты извлекаешь комплектацию автомобиля из объявления
и переводишь её на русский для публикации в Telegram. Объявление приходит
на немецком, французском, английском или русском языке.

Источников два, и пройти надо оба целиком: короткий список оборудования
с сайта и описание продавца. В описании почти всегда есть то, чего нет
в списке, — аудиосистема, камеры, отделка салона, диски, содержимое
пакетов. Пропускать его нельзя: это самое ценное в комплектации.

Работай по описанию строка за строкой, сверху вниз, в том же порядке,
в каком оно написано, — так ничего не потеряется. Порядок в ответе значения
не имеет, список всё равно будет пересортирован.

Правила:
1. Используй ТОЛЬКО те опции и оборудование, которые прямо указаны в объявлении.
2. Ничего не додумывай и не добавляй от себя, даже если опция обычно входит
   в пакет или стандартную комплектацию модели.
3. Не определяй комплектацию по модели, году выпуска или названию пакета,
   если конкретная функция прямо не указана в тексте.
4. Не превращай одну опцию в другую и не расширяй её смысл. Перевод должен
   максимально точно соответствовать оригиналу.
5. Одна функция — одна строка. Объявление описывает её по нескольку раз
   разными словами («Sitzheizung vorn» и «Sitzheizung für Fahrer und
   Beifahrer», «Rekuperationssystem» и «Bremsenergierückgewinnung») —
   выведи такую функцию ОДИН раз, самой понятной формулировкой.
   Если функция есть и в списке оборудования, и в описании продавца, бери
   готовую строку из списка оборудования слово в слово: перевод описания
   рядом с ней будет выглядеть как две разные опции.
   Пакет и его же элементы — тоже одна строка: назвав «Пакет M Sport»,
   не выводи «внешние элементы M Sport» и «специфические элементы
   M Sportpaket» отдельно.
6. Не включай: контакты продавца, адреса, условия финансирования и лизинга,
   юридические оговорки, рекламный текст, историю дилера, цены и скидки,
   сервисную информацию, приглашения приехать и позвонить.
   Не включай и мелочь, которую покупатель не выбирает: обивку стоек, рам
   и порогов, крючки, решётки, воздуховоды, число дверей, колёсную базу,
   свес, грузоподъёмность оси. Коврики, потолок и обивку сидений — включай,
   это видимая отделка.
7. Коды заводских опций (цифры и буквы в скобках) не выводи — только
   понятное русское название оборудования.
8. Названия фирменных систем и пакетов сохраняй, когда это важно:
   BMW Live Cockpit Professional, Driving Assistant Professional,
   Parking Assistant Professional, M Sport, Harman/Kardon, Burmester,
   Travel & Comfort System и подобные.
9. Если после названия пакета в объявлении прямо перечислено его содержимое,
   выведи подтверждённые опции этого пакета отдельными пунктами.
10. Не добавляй функции пакета, которые не перечислены в объявлении.
11. Технические обозначения ABS, ACC, DAB, xDrive, DSC, ISOFIX, LED
    сохраняй в привычном виде.
12. Переводи полностью. В строке не должно остаться немецких, английских
    или голландских слов — кроме фирменных названий из пункта 8 и
    обозначений из пункта 11. «Shadow-Line Hochglanz» → «глянцевая отделка
    Shadow Line», «Doppelspeiche Bicolor Schwarzgrau» → «двухцветные
    черно-серые диски», «Durchladeeinrichtung» → «люк в спинке заднего
    сиденья».
13. Служебную приставку категории до двоеточия выбрасывай:
    «Fahrassistenz-System: Park-Assistent» → «Ассистент парковки»,
    «Innenausstattung: Interieurleisten M» → «Интерьерные вставки M».
14. Формулировки короткие, как в автомобильном объявлении: не длиннее
    55 символов, с заглавной буквы, без точки в конце. Если оригинал
    длиннее — оставь суть, отбрось уточнения и перечисления.
15. Если в объявлении явно указано отсутствие функции («ohne», «without»,
    «Entfall», «нет»), не выдавай её как имеющуюся опцию.
16. Точность важнее полноты: лучше пропустить сомнительную опцию,
    чем добавить то, чего продавец прямо не подтвердил.

Формат ответа — по одной опции в строке, две части через {SEP}:

Русское название{SEP}фрагмент объявления, дословно

Фрагмент — точная цитата из присланного текста, по которой видно, что опция
есть. Строки без дословной цитаты будут отброшены автоматически.
Никакой нумерации, дефисов, markdown, заголовков и пояснений."""


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


def _norm(text: str) -> str:
    """Текст для сверки: без регистра, знаков и лишних пробелов."""
    return " ".join(re.findall(r"[^\W_]+", (text or "").lower(), re.UNICODE))


def _confirmed(quote: str, source: str) -> bool:
    """
    Есть ли цитата в объявлении. Дословное совпадение — обычный случай;
    если модель слегка переставила слова, засчитываем по словам: почти все
    должны найтись в тексте. Полностью выдуманную опцию так не подтвердить.
    """
    q = _norm(quote)
    if not q:
        return False
    if q in source:
        return True
    words = [w for w in q.split() if len(w) >= 3]
    if not words:
        return False
    hits = sum(1 for w in words if w in source)
    return hits >= max(1, round(len(words) * 0.8))


def _sort_key(line: str) -> tuple[int, str]:
    """Алфавит: сначала латиница, потом кириллица, цифры в самый конец."""
    first = line[:1]
    return (1 if first.isdigit() else 0, line.lower())


def _clean_name(text: str) -> str:
    """Название опции без маркеров списка, markdown, кодов и значков ®."""
    name = _MARKER_RE.sub("", text.strip())
    name = re.sub(r"\*\*|__", "", name)
    name = _CODE_RE.sub("", name)
    name = name.replace("®", "").replace("™", "")
    return re.sub(r"\s{2,}", " ", name).strip(" .,;")


def _drop_repeated_prefixes(names: list[str]) -> list[str]:
    """
    Убирает служебную приставку категории: «Система помощи водителю: Driving
    Assistant Plus» → «Driving Assistant Plus».

    Отличаем приставку от смысла по повторяемости: заголовок раздела
    объявления попадает сразу в несколько строк, а «Обивка сидений:» —
    в одну, и её трогать не надо.
    """
    counts: dict[str, int] = {}
    for name in names:
        head, sep, rest = name.partition(": ")
        if sep and rest:
            counts[head.lower()] = counts.get(head.lower(), 0) + 1

    out = []
    for name in names:
        head, sep, rest = name.partition(": ")
        if sep and rest and counts.get(head.lower(), 0) > 1:
            name = rest[:1].upper() + rest[1:]
        out.append(name)
    return out


def _shorten(name: str) -> str:
    """Слишком длинную строку укорачиваем за счёт уточнений в скобках."""
    if len(name) <= 55:
        return name
    short = re.sub(r"\s*\([^()]*\)", "", name).strip(" ,;")
    return short if len(short) >= 12 else name


def _key_words(text: str) -> set[str]:
    """Огрубленные слова строки — по ним ищем повторы формулировок."""
    return {w[:4] for w in _norm(text).split() if len(w) >= 3}


def _dedupe(options: list[str]) -> list[str]:
    """
    Одна и та же опция в двух формулировках («Потолок антрацит» и «Потолок
    Individual антрацит») — оставляем более подробную. Списки оборудования
    и описание продавца пересекаются, без этого КП вдвое длиннее.

    Короткие строки сверяем целиком: у «Спортивного пакета» и «Спортивной
    подвески» общее слово одно, но это разные опции. Длинным хватает
    совпадения по большинству слов — они различаются уточнениями.
    """
    kept: list[str] = []
    kept_words: list[set[str]] = []
    for opt in sorted(options, key=len, reverse=True):
        words = _key_words(opt)
        if not words:
            continue
        need = len(words) if len(words) <= 3 else round(len(words) * 0.6)
        if any(len(words & prev) >= need for prev in kept_words):
            continue
        kept.append(opt)
        kept_words.append(words)
    return kept


def _add_missing(options: list[str], features: list[str], quotes: list[str]) -> list[str]:
    """
    Дописывает опции чек-листа, которых модель не назвала.

    Чек-лист сайта уже по-русски и по определению точен, а модель, увлёкшись
    описанием продавца, способна пропустить пневмоподвеску или массаж сидений.
    Описание добавляет строки, но ничего не отменяет.

    Строку, которую модель процитировала, считаем названной, даже если
    перевела своими словами: иначе «Руль с подогревом» из чек-листа встанет
    рядом с «Обогревом рулевого колеса» из описания.
    """
    quoted = " | ".join(_norm(q) for q in quotes)
    out = list(options)
    seen_words = [_key_words(o) for o in options]
    for f in features:
        words = _key_words(f)
        if not words or _norm(f) in quoted:
            continue
        need = len(words) if len(words) <= 3 else round(len(words) * 0.6)
        if any(len(words & prev) >= need for prev in seen_words):
            continue
        out.append(f)
        seen_words.append(words)
    return out


def _parse_answer(text: str, source: str) -> tuple[list[str], list[str]]:
    """
    Разбор ответа модели: перевод + цитата, неподтверждённое выбрасываем.
    Возвращает (опции, процитированные фрагменты объявления).
    """
    out: list[str] = []
    quotes: list[str] = []
    seen: set[str] = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        name, _, quote = line.partition(SEP)
        name = _clean_name(name)
        quote = quote.strip()
        if not name or not quote:
            continue                      # без цитаты подтвердить нечем
        if not _confirmed(quote, source):
            continue
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
        quotes.append(quote)

    out = [_shorten(n) for n in _drop_repeated_prefixes(out)]
    return sorted(_dedupe(out), key=_sort_key), quotes


def _sources(d: dict) -> tuple[list[str], str]:
    """Что показываем модели: список оборудования и описание продавца."""
    features = _prepare(d.get("features", []) or [])
    description = (d.get("description") or "").strip()
    return features, description


async def build_options(d: dict) -> list[str]:
    """
    Комплектация для КП. Если ИИ недоступен или ответил невнятно, отдаём
    список оборудования как есть: у mobile.de/ru и autoscout24.ru он и так
    по-русски, а выдумок в нём быть не может по определению.
    """
    # Уже посчитано в предпросмотре — второй раз модель не гоняем
    cached = d.get("kp_options")
    if isinstance(cached, list) and cached:
        return [str(x) for x in cached]

    features, description = _sources(d)
    if not features and not description:
        return []

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return features

    parts = []
    if features:
        parts.append("Список оборудования из объявления:\n" + "\n".join(features))
    if description:
        parts.append("Описание продавца:\n" + description)
    user_prompt = "\n\n".join(parts)

    # Разовая ошибка сети или 429 не должна тихо превращать КП в сырой
    # чек-лист — пробуем дважды и в любом случае пишем причину в журнал
    answer = ""
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
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
                        # Без рассуждения модель скользит по длинному описанию
                        # и теряет строки: на живом Mercedes 4 попадания из 7
                        # против 7 из 7. Тысячи токенов хватает, больше не даёт
                        # ничего. Модели без этого режима параметр игнорируют.
                        "reasoning": {"max_tokens": 1024},
                    },
                )
                resp.raise_for_status()
                answer = resp.json()["choices"][0]["message"]["content"].strip()
            if answer:
                break
            log.warning("ИИ вернул пустой ответ (попытка %s из 2)", attempt)
        except Exception as exc:
            log.warning("ИИ не ответил (попытка %s из 2): %s", attempt, exc)
        if attempt == 1:
            await asyncio.sleep(2)

    if not answer:
        log.error("Комплектация собрана без ИИ: в КП уйдёт список оборудования как есть")
        return features

    options, quotes = _parse_answer(answer, _norm(user_prompt))
    if len(options) < 5:
        log.error(
            "ИИ вернул %s строк комплектации из %s — в КП уйдёт список оборудования",
            len(options), len(answer.splitlines()),
        )
        return features

    return sorted(_add_missing(options, features, quotes), key=_sort_key)
