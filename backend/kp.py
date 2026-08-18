"""
Сборка текста КП по шаблону + укладка в лимит подписи Telegram.

Шаблон (образец заказчика):

    ДОСТУПЕН В ЕВРОПЕ 🇪🇺

    🟢 Skoda Superb Combi 1.5 TSI DSG Ambition

    2022 / 31 000 км / 1.5 150 / Бензин

    Комплектация:
    …опции…

    💸3 000 000 руб.
    под ключ в МСК (включая прямую таможню и льготный утиль)

    Связаться:
    @Aleksandr_Montaro

    #679
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

BULLET = "* "

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


def build_specs_line(d: dict) -> str:
    """«2022 / 31 000 км / 1.5 150 / Бензин» — пропускаем то, чего нет."""
    parts: list[str] = []

    year = str(d.get("year") or "").strip()
    if year:
        parts.append(year)

    mileage = d.get("mileage")
    if mileage is None:
        pass
    elif mileage == 0:
        parts.append("новый")
    else:
        parts.append(f"{fmt_thousands(mileage)} км")

    engine = d.get("engine_l")
    power = d.get("power_hp")
    if engine and power:
        parts.append(f"{engine} {power}")
    elif engine:
        parts.append(str(engine))
    elif power:
        parts.append(f"{power} л.с.")

    fuel = (d.get("fuel") or "").strip()
    if fuel:
        parts.append(fuel)

    return " / ".join(parts)


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
) -> list[str]:
    """
    Собирает КП. Обычно это одно сообщение — подпись к фото.

    Если комплектация в подпись не влезает, режем на два сообщения:
    первое (подпись к фото) — шапка и начало комплектации, второе —
    её продолжение с ценой и контактами. Склеенные подряд, они дают
    ровно тот же текст: ничего не теряется и не повторяется.
    """
    direction = d.get("direction", "minsk")
    title = html.escape(car_title(d))
    specs = build_specs_line(d)
    footer = PRICE_FOOTERS.get(direction, PRICE_FOOTERS["minsk"])
    if direction in ("kult40", "msk") and not util_reduced:
        footer = FULL_UTIL_FOOTER

    # Эмодзи марки — справа от названия: слева он отжимает название от края
    car_line = f"{title} {_emoji_tag(brand_emoji_id, brand_emoji_fallback)}"
    price_line = f"{_emoji_tag(price_emoji_id, '💸')}{fmt_price_rub(total_rub)}"

    header = f"ДОСТУПЕН В ЕВРОПЕ {_emoji_tag(header_emoji_id, header_emoji_fallback)}"
    head = [header, "", car_line]
    if specs:
        head += ["", specs]

    tail = ["", price_line, footer, "", "Связаться:", contact, "", f"#{lot_number}"]

    def bullets(opts: list[str]) -> list[str]:
        return [BULLET + html.escape(o) for o in opts]

    def wrap(lines: list[str]) -> str:
        # Весь текст КП жирный — одной обёрткой, без вложенных тегов внутри
        return "<b>" + "\n".join(lines) + "</b>"

    opts = list(options)
    body = ["", "Комплектация:"] + bullets(opts) if opts else []
    whole = wrap(head + body + tail)
    if tg_len(whole) <= SAFE_LIMIT:
        return [whole]

    # Первая часть — сколько опций поместится в подпись к фото
    fit = len(opts)
    while fit > 0:
        first = wrap(head + ["", "Комплектация:"] + bullets(opts[:fit]))
        if tg_len(first) <= SAFE_LIMIT:
            break
        fit -= 1

    # Вторая — всё остальное вместе с ценой и контактами
    rest = opts[fit:]
    second = wrap(bullets(rest) + tail)
    while rest and tg_len(second) > SAFE_MESSAGE_LIMIT:
        rest.pop()
        second = wrap(bullets(rest) + tail)

    return [first, second]
