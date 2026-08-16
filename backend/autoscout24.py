"""
Парсер объявлений autoscout24 (.ru / .com / .de и прочие домены).

Публичная точка входа — scrape_autoscout24(url). Возвращает тот же словарь,
что и scraper._parse_html:
    source, photos, all_photos, price_eur, make, model, title, year, mileage,
    color, features, power_hp, engine_l, fuel, gearbox

Все данные страницы лежат в <script id="__NEXT_DATA__">, поэтому HTML как таковой
не разбирается — достаточно вытащить JSON и пройти по props.pageProps.listingDetails.
"""
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from scraper import (
    SOURCE_AUTOSCOUT,
    fetch_html,
    normalise_fuel,
    normalise_gearbox,
    parse_engine_litres,
    parse_power_hp,
)

# Маркеры защиты DataDome: если они в HTML — страница не отдалась.
BLOCKED_MARKERS = (
    "Access Denied",
    "captcha-delivery",
    "Доступ запрещен",
    "geo.captcha-delivery.com",
    "Zugriff verweigert",
)

# Порядок категорий опций: сначала то, что интереснее клиенту.
_EQUIPMENT_ORDER = (
    "extras",
    "comfortAndConvenience",
    "safetyAndSecurity",
    "entertainmentAndMedia",
)

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)


# ── Вспомогательное: безопасный доступ и приведение типов ────────────────────

def _dig(data, *path, default=None):
    """
    Безопасный проход по вложенным словарям: _dig(d, "props", "pageProps").
    Любой промах (нет ключа, None, не словарь) даёт default вместо исключения.
    """
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _as_text(value) -> str:
    """Строка из чего угодно; None и не-скаляры → пустая строка."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _as_int(value) -> int | None:
    """Целое из числа или строки («73 489 км» → 73489); иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    digits = re.sub(r"\D", "", _as_text(value))
    return int(digits) if digits else None


def _as_float(value) -> float | None:
    """Дробное из числа или строки («€ 28 940» → 28940.0); иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    num = _as_int(value)
    return float(num) if num is not None else None


def _formatted(value) -> str:
    """
    Поля вида {"raw": 2, "formatted": "Супер 95"} — берём человекочитаемое.
    Если пришла обычная строка — отдаём её же.
    """
    if isinstance(value, dict):
        return _as_text(value.get("formatted")) or _as_text(value.get("raw"))
    return _as_text(value)


def _clean_spaces(text: str) -> str:
    return re.sub(r"\s{2,}", " ", (text or "").replace("\xa0", " ")).strip()


# ── Извлечение JSON из страницы ──────────────────────────────────────────────

def _extract_next_data(html: str) -> dict:
    """Достаёт и парсит содержимое <script id="__NEXT_DATA__">. При неудаче — {}."""
    raw = ""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag:
            raw = tag.string or tag.get_text() or ""
    except Exception:
        raw = ""

    if not raw:
        # Фолбэк на случай, если html.parser споткнулся о разметку.
        m = _NEXT_DATA_RE.search(html or "")
        if m:
            raw = m.group(1)

    if not raw.strip():
        return {}

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# ── Разбор отдельных полей ───────────────────────────────────────────────────

def to_jpeg_url(url: str) -> str:
    """
    autoscout24 отдаёт превью в webp, а Telegram webp как фотографию не
    принимает — КП уходит без фото. Тот же адрес с .jpg отдаёт jpeg.
    """
    if url.endswith(".webp"):
        return url[: -len(".webp")] + ".jpg"
    return url


def _parse_photos(listing: dict) -> list[str]:
    """
    Фото галереи. У autoscout24 images — готовые абсолютные ссылки
    в порядке галереи, мусорных картинок там нет.
    """
    images = listing.get("images")
    if not isinstance(images, list):
        images = []

    urls: list[str] = []
    seen: set[str] = set()
    for item in images:
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            url = _as_text(item.get("src") or item.get("url") or item.get("large"))
        else:
            url = ""
        url = to_jpeg_url(url)
        if url.startswith("http") and url not in seen:
            seen.add(url)
            urls.append(url)

    if not urls:
        header = _as_text(listing.get("headerImage"))
        if header.startswith("http"):
            urls.append(to_jpeg_url(header))
    return urls


def _parse_price(listing: dict) -> float | None:
    """Цена в евро: сначала сырое число, потом форматированные строки."""
    for path in (
        ("prices", "public", "priceRaw"),
        ("prices", "dealer", "priceRaw"),
        ("prices", "public", "price"),
        ("prices", "dealer", "price"),
        ("price", "priceFormatted"),
    ):
        value = _dig(listing, *path)
        price = _as_float(value)
        if price:
            return price
    return None


def _parse_version(vehicle: dict) -> str:
    """
    Версия/комплектация. В modelVersionInput продавцы часто дописывают мусор
    через «|» («45 TFSI e Advanced edition | Stoelverwarming | Nav») — режем.
    """
    version = _as_text(vehicle.get("modelVersionInput")) or _as_text(
        vehicle.get("modelVersionCustom")
    )
    if "|" in version:
        version = version.split("|", 1)[0]
    version = _clean_spaces(version)
    if not version:
        version = _clean_spaces(_as_text(vehicle.get("motorTypeName")))
    return version


def _parse_year(vehicle: dict) -> str:
    """Год первой регистрации: «2022-12-01» → «2022», фолбэк «12/2022» → «2022»."""
    raw = _as_text(vehicle.get("firstRegistrationDateRaw"))
    m = re.match(r"(\d{4})", raw)
    if m:
        return m.group(1)

    formatted = _as_text(vehicle.get("firstRegistrationDate"))
    m = re.search(r"(\d{4})\s*$", formatted)
    if m:
        return m.group(1)

    for key in ("productionYear", "firstRegistrationDate", "firstRegistrationDateRaw"):
        m = re.search(r"(19|20)\d{2}", _as_text(vehicle.get(key)))
        if m:
            return m.group(0)
    return ""


def _parse_engine_litres(vehicle: dict, title: str) -> float | None:
    """Объём в литрах: 1395 см³ → 1.4."""
    ccm = _as_int(vehicle.get("rawDisplacementInCCM")) or _as_int(
        vehicle.get("rawCylinderCapacity")
    )
    if ccm and 600 <= ccm <= 9000:
        return round(ccm / 1000, 1)
    return parse_engine_litres(
        _as_text(vehicle.get("displacementInCCM")),
        _as_text(vehicle.get("cylinderCapacity")),
        title,
    )


def _parse_fuel(vehicle: dict) -> str:
    """
    Тип топлива. На .ru fuelCategory уже по-русски («Электро/бензин»),
    на .de/.com — «Elektro/Benzin», «Electric/Gasoline». Парную запись
    (электричество + ДВС) normalise_fuel не ловит, поэтому ловим сами.
    """
    category = _formatted(vehicle.get("fuelCategory"))
    primary = _formatted(vehicle.get("primaryFuel"))

    low = category.lower()
    electric = any(w in low for w in ("электр", "electric", "elektro"))
    combustion = any(
        w in low
        for w in ("бензин", "petrol", "benzin", "gasoline", "дизель", "diesel")
    )
    if electric and combustion:
        return "Гибрид"

    return normalise_fuel(category) or normalise_fuel(primary)


def _parse_features(vehicle: dict) -> list[str]:
    """
    Плоский список опций без дублей. equipment — словарь категорий,
    каждая категория — список объектов вида
    {"id": "Круиз-контроль", "categoryName": "Kомфорт", "categoryId": "..."}.
    """
    equipment = vehicle.get("equipment")
    if not isinstance(equipment, dict):
        return []

    # Сначала известные категории в нужном порядке, затем всё остальное.
    keys = [k for k in _EQUIPMENT_ORDER if k in equipment]
    keys += [k for k in equipment if k not in _EQUIPMENT_ORDER]

    features: list[str] = []
    seen: set[str] = set()
    for key in keys:
        items = equipment.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                name = _as_text(item.get("id") or item.get("name") or item.get("label"))
            else:
                name = _as_text(item)
            name = _clean_spaces(name)
            if name and name not in seen:
                seen.add(name)
                features.append(name)
    return features


# ── Разбор страницы целиком ──────────────────────────────────────────────────

def parse_autoscout24_html(html: str) -> dict:
    """
    Синхронный разбор сохранённого HTML — вынесен отдельно, чтобы тестировать
    без сети. Любое поле может отсутствовать: тогда в словаре пусто/None.
    """
    data = _extract_next_data(html)
    listing = _dig(data, "props", "pageProps", "listingDetails", default={})
    if not isinstance(listing, dict):
        listing = {}
    vehicle = listing.get("vehicle")
    if not isinstance(vehicle, dict):
        vehicle = {}

    make = _clean_spaces(_as_text(vehicle.get("make")))
    model = _clean_spaces(
        _as_text(vehicle.get("model")) or _as_text(vehicle.get("modelGroup"))
    )
    version = _parse_version(vehicle)
    title = _clean_spaces(" ".join(p for p in (make, model, version) if p))

    photos = _parse_photos(listing)

    return {
        "source":     SOURCE_AUTOSCOUT,
        "photos":     photos,
        "all_photos": list(photos),   # у autoscout24 мусорных картинок нет
        "price_eur":  _parse_price(listing),
        "make":       make,
        "model":      model,
        "title":      title,
        "year":       _parse_year(vehicle),
        "mileage":    _as_int(vehicle.get("mileageInKmRaw"))
                      or _as_int(vehicle.get("mileageInKm")),
        "color":      _clean_spaces(
                          _as_text(vehicle.get("bodyColor"))
                          or _as_text(vehicle.get("bodyColorOriginal"))
                          or _as_text(vehicle.get("bodyColorRaw"))
                      ),
        "features":   _parse_features(vehicle),
        "power_hp":   _as_int(vehicle.get("rawPowerInHp"))
                      or parse_power_hp(_as_text(vehicle.get("powerInHp"))),
        "engine_l":   _parse_engine_litres(vehicle, title),
        "fuel":       _parse_fuel(vehicle),
        "gearbox":    normalise_gearbox(_formatted(vehicle.get("transmissionType"))),
    }


# ── Загрузка страницы ────────────────────────────────────────────────────────

def _warmup_url(url: str) -> str:
    """
    Корень того же домена, что и в ссылке:
    https://www.autoscout24.ru/predlozheniya/... → https://www.autoscout24.ru/
    Локаль сайта определяется доменом, поэтому подменять его нельзя.
    """
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/"
    except ValueError:
        pass
    return "https://www.autoscout24.ru/"


# ── Публичная точка входа ────────────────────────────────────────────────────

# Куски, по которым видно, что страница объявления доехала целиком
READY_MARKERS = ("__NEXT_DATA__", "listingDetails")


async def scrape_autoscout24(url: str) -> dict:
    """Загружает и разбирает объявление autoscout24 (ссылка любого домена)."""
    html = await fetch_html(
        url,
        warmup_url=_warmup_url(url),
        blocked_markers=BLOCKED_MARKERS,
        ready_markers=READY_MARKERS,
    )
    data = parse_autoscout24_html(html)

    # Цены нет — страница пришла неполной, пробуем браузером
    if not data.get("price_eur"):
        html = await fetch_html(
            url,
            warmup_url=_warmup_url(url),
            blocked_markers=BLOCKED_MARKERS,
            ready_markers=READY_MARKERS,
            prefer_http=False,
        )
        data = parse_autoscout24_html(html)
    return data
