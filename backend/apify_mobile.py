"""
Запасной путь к mobile.de через Apify.

mobile.de блокирует адрес нашего сервера (Akamai, «Access Denied»), поэтому
объявление, которое не удалось открыть напрямую, забираем готовым актором
на Apify: он ходит через свои резидентные прокси и отдаёт JSON.

Два актора на выбор (настройка apify_actor в мини-аппе):
  • 3x1t/mobile-de-scraper — быстрее (10–40 с) и дешевле (~$0.002);
  • blackfalcondata/mobile-de-scraper — $0.005, сообщает «объявление снято».
Если выбранный не вернул объявление, пробуем второй. Токен лежит в настройках
бота (apify_token), не в коде и не в .env.
"""
import logging
import re

import httpx

log = logging.getLogger("autokp.apify")

ACTOR_SETTING = "apify_actor"
TOKEN_SETTING = "apify_token"
RUN_TIMEOUT = 180   # секунд на запуск актора — с учётом очереди на стороне Apify


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
        "hu": "Новые" if (it.get("huStatus") or "").lower() == "new" else (it.get("huStatus") or ""),
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


# ── Ответ актора 3x1t ───────────────────────────────────────────────────────

_BODY_3X1T = {
    "suv": "Внедорожник/Пикап", "off-road": "Внедорожник/Пикап", "pickup": "Внедорожник/Пикап",
    "estate": "Универсал", "saloon": "Седан", "limousine": "Седан", "small car": "Малолитражка",
    "sports car": "Спорткар/Купе", "coupe": "Спорткар/Купе", "cabriolet": "Кабриолет/Родстер",
    "roadster": "Кабриолет/Родстер", "van": "Минивэн", "minibus": "Минивэн",
}


def _num_from(text: str) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def _climate(raw: str) -> str:
    """«Automatic climatisation, 2 zones» → «Климат-контроль 2-зонный»."""
    low = (raw or "").lower()
    if not low:
        return ""
    zones = re.search(r"(\d)\s*zone", low)
    if "automatic" in low:
        return f"Климат-контроль {zones.group(1)}-зонный" if zones else "Климат-контроль"
    if "air condition" in low:
        return "Кондиционер"
    return ""


def to_scraped_3x1t(it: dict) -> dict:
    """Ответ 3x1t → тот же словарь, что и у разбора страницы mobile.de."""
    from scraper import (SOURCE_MOBILE_DE, normalise_fuel, normalise_gearbox,
                         parse_engine_litres, parse_power_hp)

    a = {k: str(v) for k, v in (it.get("attributes") or {}).items() if v not in (None, "")}
    reg = a.get("First Registration", "")
    year = reg.split("/")[-1] if "/" in reg else ""
    ccm = _num_from(a.get("Cubic Capacity", ""))
    power = a.get("Power", "")
    ps = parse_power_hp(power.replace("hp", "PS"))
    kw = _num_from(power.split("kW")[0]) if "kW" in power else None

    color_raw = a.get("Colour", "")
    color = _ru_color(color_raw)
    if "metallic" in (a.get("Colour (Manufacturer)", "") + color_raw).lower() and "металлик" not in color:
        color = f"{color} металлик".strip()

    interior = ""
    if a.get("Interior Design"):
        parts = [x.strip() for x in a["Interior Design"].split(",")]
        material = _MATERIAL.get(parts[0].lower(), parts[0])
        interior = ", ".join(x for x in (material, *(_ru_color(x) for x in parts[1:])) if x)

    body_raw = a.get("Category", "").lower()
    body = next((ru for en, ru in _BODY_3X1T.items() if en in body_raw), a.get("Category", ""))
    owners = _num_from(next((v for k, v in a.items() if "owner" in k.lower()), ""))

    attrs = {
        "firstRegistration": reg,
        "category": body,
        "cubicCapacity": f"{ccm:,} ccm".replace(",", " ") if ccm else "",
        "power": f"{kw} кВт ({ps} л. с.)" if kw and ps else "",
        "fuel": a.get("Fuel", ""),
        "transmission": a.get("Transmission", ""),
        "numSeats": a.get("Number of Seats", ""),
        "doorCount": a.get("Door Count", ""),
        "numberOfPreviousOwners": str(owners) if owners else "",
        "damageCondition": _condition(a.get("Vehicle condition", "")),
        "interior": interior,
        "manufacturerColorName": a.get("Colour (Manufacturer)", ""),
        "color": color,
        "countryVersion": _origin(a.get("Origin", "")),
        "hu": "Новые" if a.get("HU", "").lower() == "new" else a.get("HU", ""),
        "emissionClass": a.get("Emission Class", ""),
        "climatisation": _climate(a.get("Climatisation", "")),
        "trimLine": a.get("Trim line", ""),
    }
    attrs = {k: v for k, v in attrs.items() if v}
    price = ((it.get("price") or {}).get("total") or {}).get("amount")
    photos = [_photo(u) for u in (it.get("images") or []) if u]

    return {
        "source": SOURCE_MOBILE_DE,
        "photos": photos,
        "all_photos": photos,
        "price_eur": float(price) if price else None,
        "make": it.get("brand") or "",
        "model": it.get("model") or "",
        "title": re.sub(r"\s{2,}", " ", it.get("title") or "").strip(),
        "year": str(year),
        "mileage": _num_from(a.get("Mileage", "")),
        "color": color,
        "features": list(it.get("features") or []),
        "description": it.get("description") or "",
        "power_hp": ps,
        "engine_l": parse_engine_litres(f"{ccm} ccm") if ccm else None,
        "fuel": normalise_fuel(a.get("Fuel", "")),
        "gearbox": normalise_gearbox(a.get("Transmission", "")),
        "country_version": "ЕС" if "ЕС" in attrs.get("countryVersion", "") else "",
        "attrs": attrs,
        "fetched_via": "apify:3x1t",
    }


# ── Акторы ──────────────────────────────────────────────────────────────────

ACTORS = {
    "3x1t": {
        "id": "5rqwyghNIbO6VMnlX",
        "title": "3x1t — быстрый (10–40 с, ~$0.002)",
        "input": lambda url: {"searchPageURLs": [url], "searchPageURLMaxItems": 1},
        "parse": to_scraped_3x1t,
    },
    "blackfalcon": {
        "id": "DKQ4rrHjomlGKsYIT",
        "title": "blackfalcondata — медленнее (до 3 мин, $0.005)",
        "input": lambda url: {"startUrls": [url], "maxResults": 1, "includeDetails": True,
                              "descriptionFormat": "text"},
        "parse": lambda it: {**to_scraped(it), "fetched_via": "apify:blackfalcon"},
    },
}
DEFAULT_ACTOR = "3x1t"


def selected_actor() -> str:
    try:
        from storage import get_optional
        key = (get_optional(ACTOR_SETTING) or "").strip()
    except Exception:
        key = ""
    return key if key in ACTORS else DEFAULT_ACTOR


async def _run_actor(client, key: str, listing_id: str) -> dict:
    """Один запуск актора. ListingGone — снято; Exception — не вышло."""
    import asyncio
    import time

    actor = ACTORS[key]
    r = await client.post(f"https://api.apify.com/v2/acts/{actor['id']}/runs",
                          params={"waitForFinish": 60},
                          json=actor["input"](canonical_url(listing_id)))
    if r.status_code >= 400:
        raise Exception(f"Apify ответил {r.status_code}: {r.text[:200]}")
    run = r.json()["data"]

    # Apify держит запрос не дольше 60 секунд, а запуск иногда идёт дольше —
    # опрашиваем, пока он не закончится, а не читаем пустой результат
    deadline = time.monotonic() + RUN_TIMEOUT
    while run.get("status") in ("READY", "RUNNING") and time.monotonic() < deadline:
        await asyncio.sleep(3)
        run = (await client.get(f"https://api.apify.com/v2/actor-runs/{run['id']}",
                                params={"waitForFinish": 30})).json()["data"]
    if run.get("status") in ("READY", "RUNNING"):
        raise Exception(f"{key}: не успел за {RUN_TIMEOUT} с")

    items = (await client.get(
        f"https://api.apify.com/v2/datasets/{run['defaultDatasetId']}/items")).json()
    if items:
        data = actor["parse"](items[0])
        if not data.get("price_eur"):
            raise Exception(f"{key}: в ответе нет цены")
        log.info("Apify %s: объявление %s получено, $%.4f", key, listing_id,
                 run.get("usageTotalUsd") or 0)
        return data
    status = run.get("statusMessage") or ""
    if "no longer on mobile.de" in status or "sold or removed" in status:
        raise ListingGone("Объявление уже снято с mobile.de или продано.")
    raise Exception(f"{key}: объявление не вернулось ({status or run.get('status')})")


async def fetch_listing(listing_id: str) -> dict:
    """Объявление через выбранный в настройках актор, при неудаче — через второй."""
    tok = token()
    if not tok:
        raise Exception("Apify не настроен")
    first = selected_actor()
    order = [first] + [k for k in ACTORS if k != first]
    errors = []
    # Токен в заголовке, а не в адресе: адреса запросов httpx пишет в журнал
    async with httpx.AsyncClient(timeout=90,
                                 headers={"Authorization": f"Bearer {tok}"}) as client:
        for key in order:
            try:
                return await _run_actor(client, key, listing_id)
            except ListingGone:
                raise
            except Exception as e:
                log.warning("Apify %s не помог для %s: %s", key, listing_id, e)
                errors.append(str(e))
    raise Exception("; ".join(errors))
