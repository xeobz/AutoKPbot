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
import time

import httpx

from storage import get_exclude_words

log = logging.getLogger("autokp.ai")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Модель должна читать описание продавца на немецком и переводить его точно.
# gpt-4o-mini и gpt-4.1-mini описание попросту игнорируют — проверено на живых
# объявлениях. Сменить можно секретом AI_MODEL, не трогая код.
MODEL = os.getenv("AI_MODEL") or "google/gemini-2.5-flash"

# Маркер списка в начале строки: «- », «* », «1. ». Цифры в начале названия
# («19" диски», «2-зонный климат») трогать нельзя, поэтому не strip по символам.
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
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

def _system_prompt() -> str:
    """
    Промт со стоп-словами из настроек: список правится на ходу, поэтому
    собираем его при каждом обращении, а не при загрузке модуля.
    """
    stops = get_exclude_words()
    if not stops:
        return _PROMPT_BASE
    return _PROMPT_BASE + (
        "\n\nОтдельно: не выводи перечисленное ниже, даже если оно есть"
        "\nв объявлении — это есть у любой машины и в КП только мешает:\n"
        + ", ".join(stops)
    )


_PROMPT_BASE = f"""Ты извлекаешь комплектацию автомобиля из объявления
и переводишь её на русский для публикации в Telegram. Объявление приходит
на немецком, французском, английском или русском языке.

Источник один — описание продавца. Чек-лист «Функции» с сайта не
используется совсем: там базовые пункты, которые есть у любой машины,
и в КП они только мешают. Всё ценное — аудиосистема, камеры, отделка
салона, диски, содержимое пакетов — лежит именно в описании.

Работай по описанию строка за строкой, сверху вниз. Порядок в ответе
значения не имеет, список всё равно будет пересортирован.

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
   Пакет и его же элементы — тоже одна строка: назвав «Пакет M Sport»,
   не выводи «внешние элементы M Sport» и «специфические элементы
   M Sportpaket» отдельно.
6. Выводи только то, что установлено в самой машине. Не включай ничего
   о продавце и сделке: контакты, адреса, условия финансирования, лизинга
   и кредита, гарантию любого вида (заводскую, дилерскую, продлённую,
   гарантию мобильности), сервисные и абонентские пакеты, техобслуживание,
   доставку и регистрацию, экспорт, продажу без НДС, tax-free, растаможку,
   награды и рейтинги дилера, число проданных машин, часы работы,
   юридические оговорки, рекламный текст, цены и скидки, приглашения
   приехать и позвонить.
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
    обозначений из пункта 11. Служебные слова переводи даже внутри
    фирменного названия: «Night-Paket II» → «Night пакет II»,
    «SUPERIOR Line Interieur» → «SUPERIOR Line интерьер»,
    «GUARD 360° Fahrzeugschutz» → «GUARD 360° защита автомобиля». «Shadow-Line Hochglanz» → «глянцевая отделка
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
17. Выводи только дополнительное оборудование — то, за что доплачивают
    при заказе. Всё, что стоит на любой современной машине, пропускай:
    ABS, ESP, подушки безопасности, ISOFIX, иммобилайзер, центральный
    замок, усилитель руля, электростеклоподъёмники, бортовой компьютер,
    штатное радио, Bluetooth, USB, контроль давления в шинах, аварийный
    комплект, противотуманные фары, старт-стоп.
18. Не больше 25 строк. Если ценного набралось больше, оставь то, что
    сильнее влияет на цену: пакеты, оптику, подвеску, салон, аудиосистему,
    камеры и ассистенты, диски, панораму, обогревы и вентиляцию.

Формат ответа — по одной опции в строке, две части через {SEP}:

Русское название{SEP}фрагмент объявления, дословно

Фрагмент — точная цитата из присланного текста, по которой видно, что опция
есть. Строки без дословной цитаты будут отброшены автоматически.
Никакой нумерации, дефисов, markdown, заголовков и пояснений."""


def _is_service(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in _SERVICES)


def _is_excluded(text: str, stops: list[str]) -> bool:
    """
    Есть ли в строке стоп-слово из настроек. Короткие сокращения (ABS, ESP)
    сверяем целым словом, иначе «ABS» поймает «Abstandsregelung»; длинные —
    подстрокой, чтобы ловились падежи.
    """
    low = text.lower()
    for w in stops:
        w = w.lower()
        if len(w) <= 4:
            if re.search(rf"(?<![^\W\d_]){re.escape(w)}(?![^\W\d_])", low):
                return True
        elif w in low:
            return True
    return False


# Немецкие служебные слова: строка с ними осталась непереведённой,
# даже если рядом стоит русское слово
_GERMAN = (
    " mit ", " und ", " für ", " ohne ", " inkl", " bei ", " vorn ", " hinten ",
    "fahrzeug", "ausstattung", "scheinwerfer", "lenkrad", "innenraum",
    "räder", "reifen", "verkauf", "gewähr", "garantie",
    # Цвета и «пакет» по-немецки: модель охотно оставляет их как фирменные
    "schwarz", "weiss", "weiß", "grau", "blau", "braun", "silber", "hochglanz",
    "paket", "metallic",
)


def _looks_foreign(text: str) -> bool:
    """
    Осталась ли строка на языке объявления. В КП такие строки недопустимы,
    а модель изредка копирует их из описания как есть.

    Фирменные названия (Burmester, KEYLESS-GO, MULTIBEAM LED) — одно-два
    слова без кириллицы, и они законны. Отсекаем то, где латиницы много
    или встретилось немецкое служебное слово.
    """
    low = " " + text.lower() + " "
    if any(w in low for w in _GERMAN):
        return True
    if _CYRILLIC_RE.search(text):
        return False
    return len(_LATIN_RE.findall(text)) >= 3


def _is_low_value(text: str) -> bool:
    low = text.lower()
    return any(b in low for b in _LOW_VALUE)


def _prepare(features: list[str]) -> list[str]:
    """Чистим дубли, услуги дилера и стоп-слова, базовое опускаем в конец."""
    stops = get_exclude_words()
    seen: set[str] = set()
    valuable: list[str] = []
    basic: list[str] = []

    for f in features:
        clean = re.sub(r"\s{2,}", " ", (f or "").strip())
        if not clean or len(clean) > 90:
            continue
        low = clean.lower()
        if low in seen or _is_service(clean) or _is_excluded(clean, stops):
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


# Римские цифры в названиях версий: «Night пакет II»
_ROMAN = {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii"}


def _key_words(text: str) -> set[str]:
    """
    Огрубленные слова строки — по ним ищем повторы формулировок.

    Числа и римские цифры считаем словами независимо от длины: «Night пакет»
    и «Night пакет II» — разные опции, как и 19- и 20-дюймовые диски,
    а по одним буквам они неразличимы.
    """
    return {w[:4] for w in _norm(text).split()
            if len(w) >= 3 or w.isdigit() or w in _ROMAN}


def _versions(text: str) -> set[str]:
    """
    Числа и римские цифры строки.

    Ими различаются соседние опции одного семейства: «Night пакет» и «Night
    пакет II», диски 19 и 20 дюймов. Всё остальное в них совпадает, поэтому
    без этой проверки склейка оставила бы одну строку из двух.
    """
    return {w for w in _norm(text).split() if w.isdigit() or w in _ROMAN}


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
    seen: list[tuple[set[str], set[str]]] = []
    for opt in sorted(options, key=len, reverse=True):
        words = _key_words(opt)
        if not words:
            continue
        need = len(words) if len(words) <= 3 else round(len(words) * 0.6)
        vers = _versions(opt)
        if any(len(words & prev) >= need and vers == prev_vers
               for prev, prev_vers in seen):
            continue
        kept.append(opt)
        seen.append((words, vers))
    return kept


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
    """
    Описание продавца — то, что уходит модели. Чек-лист «Функции» с сайта
    в КП не идёт совсем: там базовое, которое клиенту ничего не говорит.
    Чек-лист возвращаем только на запасной случай — когда описания нет.
    """
    features = _prepare(d.get("features", []) or [])
    description = (d.get("description") or "").strip()
    return features, description


_MERGE_PROMPT = f"""Тебе дают готовый список комплектации автомобиля для
коммерческого предложения. В нём есть пары строк про одно и то же: они
пришли из разных мест объявления и написаны разными словами.

Найди такие пары и верни их, по одной паре в строке, через {SEP}:

лишняя строка{SEP}строка про то же самое

Обе части — строки из присланного списка, слово в слово. Примеры пар:
«Обогрев лобового стекла{SEP}Ветровое стекло с подогревом»,
«Руль с подогревом{SEP}Подогрев рулевого колеса»,
«Люк в крыше{SEP}Сдвижной люк».

Разные опции парой не считай: «Спортивный пакет» и «Спортивная подвеска»,
«Подогрев сидений» и «Вентиляция сидений» — это разное, пары тут нет.
Если повторов нет, верни пустой ответ. Никаких пояснений и нумерации."""


def _brand_tokens(text: str) -> set[str]:
    """
    Фирменные названия строки: KEYLESS-GO, Burmester, Nappa, MULTIBEAM.

    КАПС считаем всегда, слово с большой буквы — только если оно не первое:
    иначе «Electric heated rear seats» сойдёт за фирменное название, хотя это
    просто непереведённая строка, которую как раз и надо выбросить.
    """
    out: set[str] = set()
    for m in _LATIN_RE.finditer(text):
        word = m.group(0)
        if word.isupper() or (word[0].isupper() and text[: m.start()].strip()):
            out.add(word.lower())
    return out


def _same_thing(a: str, b: str) -> bool:
    """
    Похожи ли строки настолько, чтобы считать их парой повторов.

    Модель охотно предлагает в пару что попало, поэтому проверяем сами:
    у настоящей пары есть общее слово («Обогрев лобового стекла» и «Ветровое
    стекло с подогревом» — «стекл»). Исключение — непереведённый остаток
    вроде «Electric heated rear seats»: с русской строкой он не пересечётся
    ни одним словом, но это тот же пункт.
    """
    if _key_words(a) & _key_words(b):
        return True
    return not _CYRILLIC_RE.search(a) or not _CYRILLIC_RE.search(b)


async def _merge_synonyms(options: list[str], api_key: str) -> list[str]:
    """
    Убирает пары про одно и то же, пришедшие из чек-листа и из описания.

    Сверять их кодом бесполезно: «Обогрев лобового стекла» и «Ветровое стекло
    с подогревом» не пересекаются ни одним словом. Поэтому пары ищет модель,
    а решает код: обе строки должны быть из списка, и вместе со строкой не
    должно пропасть фирменное название. Если его теряет одна сторона — режем
    другую, если обе — пару пропускаем.
    """
    if len(options) < 12:
        return options

    answer = await _ask(_MERGE_PROMPT, "\n".join(options), api_key, reasoning=512)
    if not answer:
        return options

    allowed = {_norm(o): o for o in options}
    limit = max(1, round(len(options) * 0.25))     # больше четверти не режем
    dropped: set[str] = set()
    survivors: set[str] = set()

    for line in answer.splitlines():
        extra, sep, stays = line.partition(SEP)
        if not sep:
            continue
        drop_key = _norm(_clean_name(extra))
        keep_key = _norm(_clean_name(stays))
        if drop_key not in allowed or keep_key not in allowed or drop_key == keep_key:
            continue
        if not _same_thing(allowed[drop_key], allowed[keep_key]):
            continue

        drop_brands = _brand_tokens(allowed[drop_key])
        keep_brands = _brand_tokens(allowed[keep_key])
        if drop_brands - keep_brands:               # выбросили бы KEYLESS-GO
            if keep_brands - drop_brands:
                continue                            # обе стороны с названиями
            drop_key, keep_key = keep_key, drop_key

        # Цепочки А→Б и Б→В унесли бы обе строки с фирменным словом
        if drop_key in dropped or keep_key in dropped or drop_key in survivors:
            continue
        dropped.add(drop_key)
        survivors.add(keep_key)
        if len(dropped) >= limit:
            log.warning("Склейка повторов предложила слишком много — обрезал на %s",
                        limit)
            break

    return [o for o in options if _norm(o) not in dropped]


async def _ask(system: str, user: str, api_key: str, reasoning: int = 1024) -> str:
    """
    Запрос к модели. Разовая ошибка сети или 429 не должна тихо превращать
    КП в сырой чек-лист — пробуем дважды и пишем причину в журнал.
    """
    for attempt in (1, 2):
        try:
            # Живой ответ приходит за 10-25 секунд; минуты — это уже зависший
            # провайдер, и ждать его дольше, чем менеджер готов ждать КП, незачем
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
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": 0.1,
                        # Без рассуждения модель скользит по длинному описанию
                        # и теряет строки: на живом Mercedes 4 попадания из 7
                        # против 7 из 7. Тысячи токенов хватает, больше не даёт
                        # ничего. Модели без этого режима параметр игнорируют.
                        "reasoning": {"max_tokens": reasoning},
                    },
                )
                resp.raise_for_status()
                # content бывает null: провайдер отдал одни рассуждения
                # или срезал ответ фильтром — это не ошибка, а повод повторить
                choices = resp.json().get("choices") or [{}]
                answer = ((choices[0].get("message") or {}).get("content") or "").strip()
            if answer:
                return answer
            log.warning("ИИ вернул пустой ответ (попытка %s из 2)", attempt)
        except Exception as exc:
            log.warning("ИИ не ответил (попытка %s из 2): %s", attempt, exc)
        if attempt == 1:
            await asyncio.sleep(2)
    return ""


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
    d["kp_options_source"] = "checklist"
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not description or not api_key:
        # Старые черновики и записи истории сохранялись до того, как парсер
        # научился брать описание. Пустая комплектация в КП хуже чек-листа,
        # поэтому отдаём его — а причина остаётся в журнале
        log.warning("Комплектация из чек-листа: описания продавца нет")
        return features

    user_prompt = "Описание продавца:\n" + description

    started = time.monotonic()
    source = _norm(user_prompt)

    # Модель изредка отвечает одной строкой вместо полутора сотен. Одна такая
    # осечка не должна превращать КП в сырой чек-лист — просим ещё раз.
    options: list[str] = []
    quotes: list[str] = []
    for attempt in (1, 2):
        answer = await _ask(_system_prompt(), user_prompt, api_key)
        if not answer:
            break
        options, quotes = _parse_answer(answer, source)
        if len(options) >= 5:
            break
        log.warning(
            "Заход %s: разобрано %s строк из %s. Начало ответа: %.200s",
            attempt, len(options), len(answer.splitlines()), answer.replace("\n", " ⏎ "),
        )

    if len(options) < 5:
        log.error("Комплектация собрана без ИИ: в КП уйдёт список оборудования как есть")
        return features

    d["kp_options_source"] = "ai"
    # Стоп-слова ловим и в ответе: описание продавца приносит их не меньше
    stops = get_exclude_words()
    options = [o for o in options
               if not _is_excluded(o, stops) and not _looks_foreign(o)]
    # Склейка повторов — необязательная косметика. Если первый заход и так
    # тянулся, второго менеджер ждать не должен: КП уйдёт с парой повторов,
    # но сейчас, а не через четыре минуты.
    if time.monotonic() - started < 40:
        options = await _merge_synonyms(options, api_key)
    else:
        log.warning("Разбор занял %.0fс — склейку повторов пропускаю",
                    time.monotonic() - started)
    return sorted(options, key=_sort_key)
