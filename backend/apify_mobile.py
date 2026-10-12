"""
Запасной путь к mobile.de через Apify.

mobile.de блокирует адрес нашего сервера (Akamai, «Access Denied»), поэтому
объявление, которое не удалось открыть напрямую, забираем готовым актором
на Apify: он ходит через свои резидентные прокси и отдаёт JSON.

Актор blackfalcondata/mobile-de-scraper, $0.005 за объявление. Токен лежит
в настройках бота (apify_token), не в коде и не в .env.
"""
import logging
import re

import httpx

log = logging.getLogger("autokp.apify")

ACTOR = "DKQ4rrHjomlGKsYIT"
TOKEN_SETTING = "apify_token"
RUN_TIMEOUT = 120   # секунд на запуск актора


class ListingGone(Exception):
    """Объявление продано или снято — актор об этом сообщает прямо."""


def token() -> str:
    try:
        from storage import get_optional
        return (get_optional(TOKEN_SETTING) or "").strip()
    except Exception:
        return ""


def canonical_url(listing_id: str) -> str:
    # Актор понимает только немецкую ссылку: русскую версию он принимает
    # за страницу поиска без фильтров и ничего не возвращает
    return f"https://suchen.mobile.de/fahrzeuge/details.html?id={listing_id}"


# ── Перевод значений на русский, как их отдаёт русская версия сайта ─────────

_COLORS = {
    "white": "Белый", "black": "Черный", "grey": "Серый", "gray": "Серый",
    "silver": "Серебристый", "blue": "Синий", "red": "Красный", "green": "Зеленый",
    "brown": "Коричневый", "beige": "Бежевый", "yellow": "Желтый", "orange": "Оранжевый",
    "gold": "Золотой", "purple": "Фиолетовый", "violet": "Фиолетовый",
}
_BODY = {
    "ESTATE": "Универсал", "ESTATECAR": "Универсал", "SALOON": "Седан", "LIMOUSINE": "Седан",
    "SEDAN": "Седан", "SUV": "Внедорожник/Пикап", "OFFROAD": "Внедорожник/Пикап",
    "SMALL_CAR": "Малолитражка", "SMALLCAR": "Малолитражка", "COUPE": "Купе",
    "SPORTS_CAR": "Спорткар/Купе", "CABRIO": "Кабриолет/Родстер", "CONVERTIBLE": "Кабриолет/Родстер",
    "VAN": "Минивэн", "MINIVAN": "Минивэн", "PICKUP": "Внедорожник/Пикап",
}
_MATERIAL = {
    "part leather": "Частично из кожи", "full leather": "Полностью из кожи",
    "leather": "Кожа", "cloth": "Ткань", "fabric": "Ткань", "alcantara": "Алькантара",
    "velour": "Велюр", "other": "Другое",
}


def _ru_color(raw: str) -> str:
    low = (raw or "").lower()
    base = next((ru for en, ru in _COLORS.items() if en in low), "")
    if not base:
        return raw or ""
    return f"{base} металлик" if "metallic" in low else base


def _condition(raw: str) -> str:
    low = (raw or "").lower()
    parts = []
    if "new" in low and "used" not in low:
        parts.append("Новый")
    elif low:
        parts.append("Подержанный")
    if "accident-free" in low or "accident free" in low:
        parts.append("Без ДТП")
    elif "damaged" in low:
        parts.append("Повреждённый")
    return ", ".join(parts)


def _origin(raw: str) -> str:
    low = (raw or "").lower()
    if "german" in low:
        return "Немецкая версия"
    if "eu" in low.split() or "european" in low or "eu " in low:
        return "Транспортное средство ЕС"
    return raw or ""


def _photo(url: str) -> str:
    # Картинки отдаются через m.mobile.de/yams-proxy, а m.mobile.de закрыт для
    # сервера. Сам хостинг фото (img.classistatic.de) открыт — идём к нему
    m = re.search(r"images/([0-9a-f]{2})/([0-9a-f-]{20,})", url or "")
    if not m:
        return url
    return f"https://img.classistatic.de/api/v1/mo-prod/images/{m.group(1)}/{m.group(2)}?rule=mo-640.jpg"


def _title(it: dict) -> str:
    head = " ".join(x for x in (it.get("make"), it.get("model")) if x)
    sub = (it.get("subTitle") or "").strip()
    title = f"{head} {sub}".strip()
    # «BMW 318 d touring» → «BMW 318d touring»: так пишет сам mobile.de
    return re.sub(r"\b(\d{3}) ([dei])\b", r"\1\2", title)


def to_scraped(it: dict) -> dict:
    """Ответ актора → тот же словарь, что отдаёт разбор страницы mobile.de."""
    from scraper import (SOURCE_MOBILE_DE, normalise_fuel, normalise_gearbox,
                         parse_engine_litres)

    reg = it.get("firstRegistration") or ""
    year = it.get("yearOfManufacture") or (reg.split("/")[-1] if "/" in reg else "")
    ccm = it.get("cubicCapacityCc")
    material = _MATERIAL.get((it.get("interiorMaterial") or "").lower(), it.get("interiorMaterial") or "")
    interior = ", ".join(x for x in (material, _ru_color(it.get("interiorColor") or "")) if x)
    color = _ru_color(it.get("colorRaw") or it.get("color") or "")
    # «Металлик» актор кладёт в заводское название цвета: «Chronosgrau Metallic»
    if "metallic" in (it.get("colorManufacturer") or "").lower() and "металлик" not in color:
        color = f"{color} металлик".strip()
    kw, ps = it.get("powerKw"), it.get("powerPs")

    attrs = {
        "firstRegistration": reg,
        "category": _BODY.get((it.get("bodyType") or "").upper(), it.get("bodyTypeRaw") or ""),
        "cubicCapacity": f"{ccm:,} ccm".replace(",", " ") if ccm else "",
        "cylinder": str(it.get("cylinders") or ""),
        "power": f"{kw} кВт ({ps} л. с.)" if kw and ps else "",
        "fuel": it.get("fuelTypeRaw") or "",
        "transmission": it.get("transmissionRaw") or "",
        "numSeats": str(it.get("seats") or ""),
        "doorCount": it.get("doors") or "",
        "numberOfPreviousOwners": str(it.get("previousOwners") or ""),
        "damageCondition": _condition(it.get("conditionRaw") or ""),
        "interior": interior,
        "manufacturerColorName": it.get("colorManufacturer") or "",
        "color": color,
        "countryVersion": _origin(it.get("origin") or ""),
        "hu": it.get("huStatus") or "",
        "emissionClass": it.get("emissionClass") or "",
        "trimLine": it.get("trimLevel") or "",
    }
    attrs = {k: v for k, v in attrs.items() if v}
    photos = [_photo(u) for u in (it.get("imageUrls") or []) if u]

    return {
        "source": SOURCE_MOBILE_DE,
        "photos": photos,
        "all_photos": photos,
        "price_eur": float(it["price"]) if it.get("price") else None,
        "make": it.get("make") or "",
        "model": it.get("model") or "",
        "title": _title(it),
        "year": str(year or ""),
        "mileage": it.get("mileageKm"),
        "color": color,
        # Чек-лист на английском; в КП он не идёт — комплектацию ИИ берёт из
        # описания продавца, а список нужен для определения привода
        "features": list(it.get("features") or []),
        "description": it.get("description") or "",
        "power_hp": ps,
        "engine_l": parse_engine_litres(f"{ccm} ccm") if ccm else None,
        "fuel": normalise_fuel(it.get("fuelTypeRaw") or it.get("fuelType") or ""),
        "gearbox": normalise_gearbox(it.get("transmissionRaw") or ""),
        "country_version": "ЕС" if "ЕС" in attrs.get("countryVersion", "") else "",
        "attrs": attrs,
        "fetched_via": "apify",
    }


async def fetch_listing(listing_id: str) -> dict:
    """Объявление через актор. ListingGone — снято; Exception — сбой Apify."""
    tok = token()
    if not tok:
        raise Exception("Apify не настроен")
    async with httpx.AsyncClient(timeout=RUN_TIMEOUT + 30) as client:
        r = await client.post(
            f"https://api.apify.com/v2/acts/{ACTOR}/runs",
            params={"token": tok, "waitForFinish": RUN_TIMEOUT},
            json={"startUrls": [canonical_url(listing_id)], "maxResults": 1,
                  "includeDetails": True, "descriptionFormat": "text"},
        )
        if r.status_code >= 400:
            raise Exception(f"Apify ответил {r.status_code}: {r.text[:200]}")
        run = r.json()["data"]
        items = (await client.get(
            f"https://api.apify.com/v2/datasets/{run['defaultDatasetId']}/items",
            params={"token": tok})).json()
        if items:
            log.info("Apify: объявление %s получено, $%.4f", listing_id,
                     run.get("usageTotalUsd") or 0)
            return to_scraped(items[0])
        status = (await client.get(f"https://api.apify.com/v2/actor-runs/{run['id']}",
                                   params={"token": tok})).json()["data"].get("statusMessage") or ""
    if "no longer on mobile.de" in status or "sold or removed" in status:
        raise ListingGone("Объявление уже снято с mobile.de или продано.")
    raise Exception(f"Apify не вернул объявление: {status or run.get('status')}")
