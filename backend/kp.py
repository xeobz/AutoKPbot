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

# Подпись под ценой — своя для каждого направления
PRICE_FOOTERS = {
    "minsk":  "под ключ в МСК (включая таможню РБ и комм. утиль)",
    "kult40": "под ключ в МСК (включая прямую таможню и льготный утиль)",
    "msk":    "под ключ в МСК (включая прямую таможню и льготный утиль)",
}

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


def build_kp_text(
    d: dict,
    total_rub: float,
    options: list[str],
    lot_number: str | int,
    contact: str,
    brand_emoji_id: str | None = None,
    brand_emoji_fallback: str = "🚗",
    price_emoji_id: str | None = None,
) -> str:
    """
    Собирает КП и укладывает его в лимит подписи: при нехватке места
    срезаются опции снизу, шапка/цена/контакты остаются всегда.
    """
    direction = d.get("direction", "minsk")
    title = html.escape(car_title(d))
    specs = build_specs_line(d)
    footer = PRICE_FOOTERS.get(direction, PRICE_FOOTERS["minsk"])

    car_line = f"{_emoji_tag(brand_emoji_id, brand_emoji_fallback)} {title}"
    price_line = f"{_emoji_tag(price_emoji_id, '💸')}{fmt_price_rub(total_rub)}"

    head = ["ДОСТУПЕН В ЕВРОПЕ 🇪🇺", "", car_line]
    if specs:
        head += ["", specs]

    tail = ["", price_line, footer, "", "Связаться:", contact, "", f"#{lot_number}"]

    def assemble(opts: list[str]) -> str:
        body = ["", "Комплектация:"] + [html.escape(o) for o in opts] if opts else []
        # Весь текст КП жирный — одной обёрткой, без вложенных тегов внутри
        return "<b>" + "\n".join(head + body + tail) + "</b>"

    opts = list(options)
    text = assemble(opts)
    while opts and tg_len(text) > SAFE_LIMIT:
        opts.pop()
        text = assemble(opts)
    return text
