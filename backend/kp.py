"""
Сборка текста КП по шаблону + укладка в лимит подписи Telegram.

Шаблон (образец заказчика):

    🏷 #1285 | Mercedes-Benz G 400 d 4MATIC AMG Line 🚙

    Год: 2022
    Пробег: 5 900 км
    Двигатель: 2.9 л дизель
    Мощность: 330 л.с.
    Привод: 4MATIC
    Цвет: серебристый
    Из Германии. Срок: 45 дней.

    Комплектация:
    ‹свёрнутый список опций›
    Полное описание, вин код и дополнительные фото по запросу

    💸18 524 000 руб.
    под ключ в МСК (включая прямую таможню и льготный утиль)

    ✈️ Заказать автомобиль

Главное видно сразу, длинная комплектация убрана в раскрывающуюся цитату.
"""
import html
import re
import unicodedata

# Лимит подписи к фото у Bot API — 1024 символа (Premium для ботов не существует).
CAPTION_LIMIT = 1024
# Небольшой запас: считаем длину приблизительно, лучше недобрать, чем упереться.
SAFE_LIMIT = 1010
# Обычное сообщение вмещает 4096 — в него уходит хвост комплектации,
# если она не влезла в подпись к фото
MESSAGE_LIMIT = 4096
SAFE_MESSAGE_LIMIT = 4000

BULLET = "– "
# Последняя строка комплектации: список в КП не исчерпывающий
MORE_LINE = "Полное описание, вин код и дополнительные фото по запросу"

# Подпись под ценой — своя для каждого направления
PRICE_FOOTERS = {
    "minsk":  "под ключ в МСК (включая таможню РБ и комм. утиль)",
    "kult40": "под ключ в МСК (включая прямую таможню и льготный утиль)",
    "msk":    "под ключ в МСК (включая прямую таможню и льготный утиль)",
}
# Льготный утиль положен не всякой машине. Если менеджер назначил свою сумму,
# обещать клиенту льготу в подписи нельзя
FULL_UTIL_FOOTER = "под ключ в МСК (включая прямую таможню и утиль)"

_TAG_RE = re.compile(r"<[^>]+>")


def tg_len(text: str) -> int:
    """
    Длина сообщения так, как её считает Telegram: без HTML-разметки и
    в UTF-16 (эмодзи занимают 2 единицы).
    """
    visible = _TAG_RE.sub("", text)
    return len(visible.encode("utf-16-le")) // 2


def fmt_thousands(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


def fmt_price_rub(v: float) -> str:
    """Итоговая цена округляется до тысяч: 2 997 400 → «3 000 000 руб.»"""
    rounded = round((v or 0) / 1000) * 1000
    return f"{fmt_thousands(rounded)} руб."


# Хвост названия из объявления: «*LED*NAV*KAM*DISTR*», «,Navi,Burmester,STH»
# — продавец набивает туда сокращения для поиска, клиенту это читать незачем
_TITLE_CUT_RE = re.compile(r"[*|•!]|\s[-–—]{2,}")


def clean_title(title: str) -> str:
    """
    Название в чистом виде: «Mercedes-Benz G 400 d 4MATIC AMG Line».

    Режем по первому служебному символу и по запятой: в названии машины
    запятых не бывает, а перечисление опций всегда начинается с неё.
    """
    name = _TITLE_CUT_RE.split(title or "", 1)[0]
    name = name.split(",")[0]
    name = re.sub(r"\s{2,}", " ", name)
    return name.strip(" .,;-/+&")


# Привод. Отдельным полем mobile.de его не отдаёт — «Тип привода» в
# карточке это тип двигателя, а не трансмиссия. Зато фирменное имя почти
# всегда стоит в названии, реже — в чек-листе или описании продавца.
_DRIVE_PATTERNS = [
    (r"4MATIC", "4MATIC"),
    (r"xDrive", "xDrive"),
    (r"sDrive", "sDrive"),
    (r"quattro", "quattro"),
    (r"4MOTION", "4MOTION"),
    (r"AllGrip", "AllGrip"),
    (r"(?<![A-Za-z0-9])4x4(?![A-Za-z0-9])", "4x4"),
    (r"(?<![A-Za-z0-9])(?:AWD|4WD)(?![A-Za-z0-9])", "полный"),
]
# У Mercedes 4MATIC часто сокращают до «4M»: «V 300d 4M EXTRALONG».
# У других марок такое сокращение значит что угодно, поэтому только здесь
_MERCEDES_4M = r"(?<![A-Za-z0-9])4M(?![A-Za-z0-9])"

_DRIVE_WORDS = (
    (("привод на четыре колеса", "полный привод", "allrad", "4wd", "awd",
      "all wheel drive", "four wheel drive"), "полный"),
    (("задний привод", "hinterrad", "rwd", "rear wheel drive"), "задний"),
    (("передний привод", "vorderrad", "fwd", "front wheel drive"), "передний"),
)


def drive_of(d: dict) -> str:
    """«4MATIC» / «полный» / «» — если в объявлении про привод ничего нет."""
    title = d.get("title") or ""
    # Название — самый надёжный источник: там имя комплектации от завода
    for pattern, name in _DRIVE_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            return name
    if "mercedes" in title.lower() and re.search(_MERCEDES_4M, title, re.IGNORECASE):
        return "4MATIC"

    # Дальше — чек-лист и описание продавца
    extra = " ".join([str(f) for f in (d.get("features") or [])]
                     + [str(d.get("description") or "")])
    for pattern, name in _DRIVE_PATTERNS:
        if re.search(pattern, extra, re.IGNORECASE):
            return name

    haystack = (title + " " + extra).lower()
    for words, name in _DRIVE_WORDS:
        if any(w in haystack for w in words):
            return name
    return ""


def build_spec_lines(d: dict, country: str = "", delivery: str = "") -> list[str]:
    """
    Характеристики отдельными строками. Чего в объявлении нет — того нет
    и в КП: пустая строка «Привод: —» выглядит как недоработка.
    """
    lines: list[str] = []

    year = str(d.get("year") or "").strip()
    if year:
        lines.append(f"Год: {year}")

    mileage = d.get("mileage")
    if mileage == 0:
        lines.append("Пробег: новый")
    elif mileage:
        lines.append(f"Пробег: {fmt_thousands(mileage)} км")

    engine = d.get("engine_l")
    fuel = (d.get("fuel") or "").strip().lower()
    if engine and fuel:
        lines.append(f"Двигатель: {engine} л {fuel}")
    elif engine:
        lines.append(f"Двигатель: {engine} л")
    elif fuel:
        lines.append(f"Двигатель: {fuel}")

    power = d.get("power_hp")
    if power:
        lines.append(f"Мощность: {power} л.с.")

    drive = drive_of(d)
    if drive:
        lines.append(f"Привод: {drive}")

    gearbox = (d.get("gearbox") or "").strip().lower()
    if gearbox:
        # normalise_gearbox отдаёт «Автомат» / «Механика» — в КП пишем словом
        name = {"автомат": "автоматическая", "механика": "механическая"}.get(gearbox, gearbox)
        lines.append(f"Коробка: {name}")

    color = (d.get("color") or "").strip()
    interior = (d.get("interior_color") or "").strip()
    if color and interior:
        lines.append(f"Цвет: {color.lower()} / салон {interior.lower()}")
    elif color:
        lines.append(f"Цвет: {color.lower()}")
    elif interior:
        lines.append(f"Салон: {interior}")

    country = (country or "").strip()
    delivery = (delivery or "").strip()
    if country and delivery:
        lines.append(f"Из {country}. Срок: {delivery}.")
    elif country:
        lines.append(f"Из {country}.")
    elif delivery:
        lines.append(f"Срок: {delivery}.")

    return lines


def car_title(d: dict) -> str:
    """Полное название объявления, с фолбэком на марку/модель."""
    title = (d.get("title") or "").strip()
    if title:
        return title
    return " ".join(
        x for x in (d.get("make", ""), d.get("model", ""), str(d.get("year", ""))) if x
    ).strip()


# Слово названия: любые буквы (включая ë, š, ü), возможно через дефис
_WORD_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)


def norm_brand(name: str) -> str:
    """
    Ключ марки: нижний регистр без диакритики, чтобы «Citroën» и «Citroen»,
    «Škoda» и «Skoda» считались одной маркой.
    """
    folded = unicodedata.normalize("NFKD", (name or "").strip().lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def brand_candidates(title: str) -> list[str]:
    """
    Возможные марки из названия — сначала два слова, потом одно:
    «Alfa Romeo Giulia» → ['alfa romeo', 'alfa'], «Skoda Superb» → ['skoda superb', 'skoda'].
    Так работают и составные марки (Alfa Romeo, Land Rover, Mercedes-Benz).
    """
    words = _WORD_RE.findall(title or "")
    out: list[str] = []
    if len(words) >= 2:
        out.append(norm_brand(f"{words[0]} {words[1]}"))
    if words:
        out.append(norm_brand(words[0]))
    return out


def brand_of(title: str) -> str:
    """Марка одним словом — используется там, где кандидатов перебирать негде."""
    cands = brand_candidates(title)
    return cands[-1] if cands else ""


def _emoji_tag(custom_emoji_id: str | None, fallback: str) -> str:
    """Премиум-эмодзи, если он задан, иначе обычный символ."""
    if custom_emoji_id:
        return f'<tg-emoji emoji-id="{custom_emoji_id}">{fallback}</tg-emoji>'
    return fallback


# Базовое, что есть у любой машины: под обрезку по лимиту подписи должно
# уходить именно оно, а не пневмоподвеска с массажем
_BASIC = (
    "abs", "esp", "esc", "asr", "isofix", "иммобилайзер", "усилитель руля",
    "гидроусилитель", "подушк", "airbag", "центральный замок", "бортовой компьютер",
    "стеклоподъемник", "стеклоподъёмник", "электростекла", "противобуксовочная",
    "старт-стоп", "start/stop", "подлокотник", "подстаканник", "тонированные стекла",
    "радио", "usb", "aux", "mp3", "bluetooth", "аварийный комплект",
    "контроль давления в шинах", "омыватель фар", "противотуманн",
    "датчик освещенности", "ручное переключение передач", "гарантия",
)


def _by_value(options: list[str]) -> list[str]:
    """Порядок обрезки: ценное впереди, базовое в хвосте. Внутри — алфавит."""
    basic = [o for o in options if any(b in o.lower() for b in _BASIC)]
    rich  = [o for o in options if o not in basic]
    return rich + basic


def _order_link(contact: str, emoji_id: str | None = None,
                emoji_fallback: str = "✈️") -> str:
    """
    Кнопкой в подписи Telegram не обойтись — делаем ссылку на менеджера.
    Эмодзи держим снаружи ссылки: внутри <a> премиум-эмодзи не отображается.
    """
    contact = (contact or "").strip()
    if contact.startswith("http"):
        url = contact
    else:
        url = "https://t.me/" + contact.lstrip("@")
    tag = _emoji_tag(emoji_id, emoji_fallback)
    return f'{tag} <a href="{html.escape(url, quote=True)}">Заказать автомобиль</a>'


def build_kp_parts(
    d: dict,
    total_rub: float,
    options: list[str],
    lot_number: str | int,
    contact: str,
    brand_emoji_id: str | None = None,
    brand_emoji_fallback: str = "🚗",
    price_emoji_id: str | None = None,
    header_emoji_id: str | None = None,
    header_emoji_fallback: str = "🇪🇺",
    util_reduced: bool = True,
    country: str = "",
    delivery: str = "",
    lot_emoji_id: str | None = None,
    lot_emoji_fallback: str = "🏷",
    order_emoji_id: str | None = None,
    order_emoji_fallback: str = "✈️",
) -> list[str]:
    """
    Собирает КП — всегда одним сообщением, подписью к фото.

    Главное (название, характеристики, цена) видно сразу, комплектация
    убрана в раскрывающуюся цитату Telegram. Если даже так не влезает
    в лимит подписи, список режется: первым уходит базовое.

    header_emoji_* больше не используются — шапки «ДОСТУПЕН В ЕВРОПЕ»
    в шаблоне нет, но аргументы остались ради вызовов из бота и веб-API.
    """
    direction = d.get("direction", "minsk")
    title = html.escape(clean_title(car_title(d)))
    footer = PRICE_FOOTERS.get(direction, PRICE_FOOTERS["minsk"])
    if direction in ("kult40", "msk") and not util_reduced:
        footer = FULL_UTIL_FOOTER

    # Номер лота — служебный, поэтому обычным текстом, а название жирным
    car_line = (f"{_emoji_tag(lot_emoji_id, lot_emoji_fallback)} #{lot_number} | "
                f"<b>{title}</b> {_emoji_tag(brand_emoji_id, brand_emoji_fallback)}")
    price_line = f"<b>{_emoji_tag(price_emoji_id, '💸')}{fmt_price_rub(total_rub)}</b>"

    top = [car_line]
    specs = build_spec_lines(d, country, delivery)
    if specs:
        top += [""] + specs

    tail = ["", price_line, footer, "",
            _order_link(contact, order_emoji_id, order_emoji_fallback)]

    def whole_of(o: list[str]) -> str:
        if not o:
            return "\n".join(top + tail)
        quoted = "\n".join(BULLET + html.escape(x) for x in o)
        body = [
            "",
            "Комплектация:",
            f"<blockquote expandable>{quoted}</blockquote>",
            MORE_LINE,
        ]
        return "\n".join(top + body + tail)

    opts = _by_value(options)

    # Даже свёрнутая комплектация считается в лимит подписи целиком:
    # блок сворачивается только визуально. Не влезло — снимаем опции
    # с конца, а конец здесь самое базовое: подушки, ABS, стеклоподъёмники
    text = whole_of(opts)
    while opts and tg_len(text) > SAFE_LIMIT:
        opts.pop()
        text = whole_of(opts)

    if len(opts) < len(options):
        # Порядок в КП всё равно алфавитный — сортируем то, что осталось
        opts.sort(key=lambda o: o.lower())
        text = whole_of(opts)

    return [text]
