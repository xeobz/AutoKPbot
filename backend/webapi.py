"""
Веб-API мини-аппа AutoKP.

Тот же функционал, что и в боте, но для веб-интерфейса: разбор ссылки,
расчёт, выбор фото, запись в Google Sheets и отправка КП в телеграм.
Расчёты и сборка КП берутся из общих модулей (calc.py / kpsend.py),
чтобы цифры и текст были ровно такими же, как в боте.

Авторизация — по подписи Telegram WebApp initData (заголовок X-Init-Data).
"""
import asyncio
import hashlib
import hmac
import html as html_lib
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telegram import Bot

load_dotenv(Path(__file__).parent.parent / ".env")

from ai import build_options
from calc import (
    DIRECTION_FIELDS,
    DIRECTION_LABELS,
    DIRECTIONS,
    apply_buyback,
    buyback_options,
    card_rows,
    default_logistics,
    netto,
    total_rub,
    util_is_reduced,
)
from kp import CAPTION_LIMIT, build_kp_parts, tg_len
from kpsend import CONTACT, build_captions, pick_photos, send_kp
from scraper import find_listing_url, scrape
from sheets import append_car_row, append_kult40_row, append_msk_row, update_car_row
from storage import (
    EDITABLE_SETTINGS,
    SECTION_TITLES,
    cleanup_drafts,
    close_draft,
    get_admin_ids,
    get_draft,
    get_float,
    get_history_by_id,
    get_history_for_user,
    get_rates,
    get_setting,
    get_tariffs,
    init_db,
    save_draft,
    save_history,
    set_rates,
    set_setting,
    update_history_data,
)

log = logging.getLogger("autokp.web")

TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEV_MODE    = os.getenv("WEB_DEV_MODE", "") == "1"
DEV_USER_ID = int(os.getenv("WEB_DEV_USER_ID", "0") or 0)
MSK         = timezone(timedelta(hours=3))
INIT_DATA_TTL = 24 * 3600      # сколько живёт подпись Telegram

_ENV_ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_TELEGRAM_ID", "").split(",")
    if x.strip().isdigit()
}

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


def _today() -> str:
    return datetime.now(MSK).strftime("%d.%m.%Y")


def _is_admin(user_id: int) -> bool:
    return user_id in _ENV_ADMIN_IDS or user_id in get_admin_ids()


# ── Авторизация Telegram WebApp ──────────────────────────────────────────────

def check_init_data(init_data: str, token: str = TOKEN) -> dict | None:
    """
    Проверяет подпись initData по алгоритму Telegram:
      secret = HMAC_SHA256("WebAppData", <токен бота>)
      hash   = HMAC_SHA256(secret, <пары ключ=значение, отсортированные, через \\n>)
    Возвращает объект пользователя или None, если подпись не сошлась.
    """
    if not init_data or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received = pairs.pop("hash", "")
    if not received:
        return None

    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None

    # Просроченную подпись не принимаем
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INIT_DATA_TTL:
            return None
    except ValueError:
        return None

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    return user if user.get("id") else None


async def current_user(x_init_data: str = Header(default="", alias="X-Init-Data")) -> dict:
    """Пользователь запроса. В dev-режиме подпись не требуется."""
    if x_init_data:
        # Подпись прислали — она обязана быть верной, даже в dev-режиме
        user = check_init_data(x_init_data)
        if not user:
            raise HTTPException(401, "Подпись Telegram не сошлась")
    elif DEV_MODE and DEV_USER_ID:
        user = {"id": DEV_USER_ID, "first_name": "Разработчик"}
    else:
        raise HTTPException(401, "Откройте приложение через Telegram")

    uid = int(user["id"])
    return {
        "id": uid,
        "name": user.get("first_name") or user.get("username") or str(uid),
        "is_admin": _is_admin(uid),
    }


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Доступно только администратору")
    return user


# ── Схемы запросов ───────────────────────────────────────────────────────────

class ParseReq(BaseModel):
    url: str


class Buyback(BaseModel):
    mode: str = "pct"        # pct | fixed
    value: float = 10


class CalcReq(BaseModel):
    draft_id: str
    direction: str = "minsk"
    vat: float = 1.19
    buyback: Buyback = Buyback()
    customs_eur: float = 0
    util_rub: float = 0
    customs_tks_rub: float = 0
    evacuator_rub: float = 0


class PreviewReq(CalcReq):
    photos: list[str] = []


class SubmitReq(PreviewReq):
    counterparty: str = ""
    with_kp: bool = True      # False — только запись в таблицу, без текста и фото


class PhotosReq(BaseModel):
    photos: list[str] = []


class SettingReq(BaseModel):
    key: str
    value: float


class RatesReq(BaseModel):
    rate_eur_usdt: float
    rate_usdt_rub: float


class HistoryEditReq(BaseModel):
    field: str
    # число (поля направления), строка (контрагент) или {mode, value} для выкупа
    value: str | float | dict


# ── Вспомогательное ──────────────────────────────────────────────────────────

def _car_payload(d: dict) -> dict:
    """Данные авто для фронта."""
    return {
        "title":     d.get("title") or d.get("car_name", ""),
        "make":      d.get("make", ""),
        "model":     d.get("model", ""),
        "year":      d.get("year", ""),
        "mileage":   d.get("mileage"),
        "color":     d.get("color", ""),
        "price_eur": d.get("price_eur"),
        "power_hp":  d.get("power_hp"),
        "engine_l":  d.get("engine_l"),
        "fuel":      d.get("fuel", ""),
        "gearbox":   d.get("gearbox", ""),
        "photos":    d.get("photos", []),
        "features":  d.get("features", []),
        "source":    d.get("source", ""),
        "url":       d.get("url", ""),
    }


def _apply_request(base: dict, req: CalcReq) -> dict:
    """Черновик + выбор менеджера → данные для расчёта."""
    if req.direction not in DIRECTIONS:
        raise HTTPException(400, f"Неизвестное направление: {req.direction}")
    # 1.0 — без НДС, 2.0 — заведомо больше любой реальной ставки
    if not (1.0 <= req.vat <= 2.0):
        raise HTTPException(400, "НДС должен быть от 0% до 100%")

    tf = get_tariffs()
    r  = get_rates()
    d  = dict(base)
    d.update({
        "direction":       req.direction,
        "vat":             req.vat,
        "logistics":       default_logistics(req.direction, tf),
        "epts_rub":        tf["epts_rub"],
        # Снимок тарифов: старые записи не должны пересчитываться после их правки
        "tariffs":         tf,
        "rate_eur_usdt":   r["rate_eur_usdt"],
        "rate_usdt_rub":   r["rate_usdt_rub"],
        "customs_eur":     req.customs_eur,
        "util_rub":        req.util_rub,
        "customs_tks_rub": req.customs_tks_rub,
        "evacuator_rub":   req.evacuator_rub,
    })
    if req.buyback.mode not in ("pct", "fixed"):
        raise HTTPException(400, "Неверный режим выкупа")
    apply_buyback(d, req.buyback.mode, req.buyback.value)
    return d


def _load_draft(draft_id: str, user: dict) -> dict:
    rec = get_draft(draft_id)
    if not rec:
        raise HTTPException(404, "Черновик не найден — начните расчёт заново")
    if rec["user_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "Это чужой расчёт")
    return rec


def _rates_payload() -> dict:
    r = get_rates()
    return {**r, "is_today": r["rates_date"] == _today()}


async def _write_row(d: dict) -> tuple[int, int]:
    """Запись строки в нужный лист (gspread блокирующий — уводим в поток)."""
    writer = {
        "kult40": append_kult40_row,
        "msk":    append_msk_row,
    }.get(d.get("direction", "minsk"), append_car_row)
    try:
        return await asyncio.to_thread(writer, d)
    except Exception as e:
        raise HTTPException(502, f"Не удалось записать в таблицу: {e}")


# ── Роуты ────────────────────────────────────────────────────────────────────

api = APIRouter()


@api.get("/me")
async def me(user: dict = Depends(current_user)):
    return {
        "user_id":  user["id"],
        "name":     user["name"],
        "is_admin": user["is_admin"],
        "rates":    _rates_payload(),
        "directions": [{"key": k, "label": DIRECTION_LABELS[k]} for k in DIRECTIONS],
    }


@api.post("/parse")
async def parse(req: ParseReq, user: dict = Depends(current_user)):
    found = find_listing_url(req.url or "")
    if not found:
        raise HTTPException(400, "Нужна ссылка с mobile.de или autoscout24")
    url, source = found

    try:
        scraped = await scrape(url)
    except Exception as e:
        log.exception("Не удалось разобрать объявление: %s", url)
        raise HTTPException(502, f"Не удалось загрузить объявление: {e}")

    if not scraped.get("price_eur"):
        raise HTTPException(422, "В объявлении не нашлась цена — проверьте ссылку")

    title = scraped.get("title") or f"{scraped.get('make','')} {scraped.get('model','')}".strip()
    data = dict(scraped)
    data.update({"url": url, "source": source, "car_name": title, "title": title})

    draft_id = uuid.uuid4().hex[:12]
    save_draft(draft_id, user["id"], user["id"], data)
    return {"draft_id": draft_id, "car": _car_payload(data)}


@api.post("/calc")
async def calc_endpoint(req: CalcReq, user: dict = Depends(current_user)):
    rec = _load_draft(req.draft_id, user)
    d   = _apply_request(rec["data"], req)
    return {
        "total_rub":        total_rub(d),
        "rows":             card_rows(d),
        "buyback_options":  buyback_options(netto(d)),
        "buyback_min_eur":  get_float("buyback_min_eur", 2500),
        # Льготный утиль — подпись на кнопке выбора у Культ40 и МСК
        "util_reduced_rub": get_float("util_fixed_rub", 5200),
        "fields":           DIRECTION_FIELDS[req.direction],
        "rates":            _rates_payload(),
    }


@api.post("/preview")
async def preview(req: PreviewReq, user: dict = Depends(current_user)):
    rec = _load_draft(req.draft_id, user)
    d   = _apply_request(rec["data"], req)

    options = await build_options(d)
    # Разбор описания моделью — самое долгое место. Кладём результат в черновик:
    # отправка не считает то же самое второй раз, а клиент получает ровно тот
    # текст, который менеджер видел в предпросмотре.
    # Запасной список (ИИ не ответил) не запоминаем: иначе одна осечка залипнет
    # в черновике, и повторная отправка выдаст тот же голый чек-лист.
    if options and d.get("kp_options_source") == "ai":
        rec["data"]["kp_options"] = options
        save_draft(req.draft_id, rec["user_id"], rec["chat_id"], rec["data"])

    # Номер лота присваивается при записи в таблицу — в предпросмотре его ещё нет
    parts = build_kp_parts(d, total_rub(d), options, "—", CONTACT,
                           util_reduced=util_is_reduced(d))
    # В предпросмотре показываем текст как его увидит клиент — без HTML-разметки
    plain = [html_lib.unescape(re.sub(r"<[^>]+>", "", p)) for p in parts]
    return {
        "parts":  plain,
        "text":   "\n".join(plain),          # для совместимости со старым фронтом
        "length": tg_len(parts[0]),
        "limit":  CAPTION_LIMIT,
    }


@api.post("/submit")
async def submit(req: SubmitReq, user: dict = Depends(current_user)):
    rec = _load_draft(req.draft_id, user)
    d   = _apply_request(rec["data"], req)

    counterparty = (req.counterparty or "").strip()
    if not counterparty:
        raise HTTPException(400, "Укажите контрагента")
    d["counterparty"] = f"{counterparty}({user['name']})"
    d["with_kp"] = req.with_kp

    car_num, sheet_row = await _write_row(d)

    save_history(
        user_id=user["id"], chat_id=rec["chat_id"], car_num=car_num, sheet_row=sheet_row,
        car_name=d.get("car_name", ""), counterparty=d.get("counterparty", ""),
        url=d.get("url", ""), data=d,
    )
    close_draft(req.draft_id)

    # Режим «без КП»: строка в таблице нужна, текст и фото — нет
    sent = await _send(rec["chat_id"], car_num, d, req.photos) if req.with_kp else False
    return {"car_num": car_num, "sheet_row": sheet_row, "sent": sent, "with_kp": req.with_kp}


async def _send(chat_id: int, car_num, d: dict, photos: list[str] | None) -> bool:
    """Отправка КП в телеграм. Ошибку отправки не считаем провалом записи."""
    bot = _bot()
    if bot is None:
        log.error("КП #%s не отправлено: бот не инициализирован", car_num)
        return False
    try:
        await send_kp(bot, chat_id, car_num, d, photos=photos or None)
        return True
    except Exception:
        log.exception("КП #%s не отправлено в чат %s", car_num, chat_id)
        return False


@api.get("/history")
async def history(limit: int = 20, user: dict = Depends(current_user)):
    items = get_history_for_user(user["id"], limit=limit)
    out = []
    for it in items:
        rec = get_history_by_id(it["id"])
        data = json.loads(rec["data_json"]) if rec else {}
        out.append({**it, "direction": data.get("direction", "minsk")})
    return out


@api.get("/history/{item_id}")
async def history_item(item_id: int, user: dict = Depends(current_user)):
    rec = get_history_by_id(item_id)
    if not rec:
        raise HTTPException(404, "Запись не найдена")
    if rec["user_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "Это чужая запись")
    data = json.loads(rec["data_json"])
    direction = data.get("direction", "minsk")
    fields = DIRECTION_FIELDS.get(direction, [])

    # Фронту удобнее плоская карточка: служебное убираем, выкуп отдаём объектом
    payload = {k: v for k, v in data.items() if k not in ("features", "all_photos")}
    payload.update({
        "car_name":   rec["car_name"],
        "car_num":    rec["car_num"],
        "created_at": rec["created_at"],
        "direction":  direction,
        "fields":     fields,
        "buyback": {
            "mode":  "fixed" if data.get("buyback_fixed") else "pct",
            "value": data.get("buyback_val") if data.get("buyback_fixed") else data.get("buyback_pct"),
        },
    })

    return {
        "id":        rec["id"],
        "car_num":   rec["car_num"],
        "car_name":  rec["car_name"],
        "created_at": rec["created_at"],
        "direction": direction,
        "data":      payload,
        "rows":      card_rows(data),
        "total_rub": total_rub(data),
        "fields":    fields,
        "car":       _car_payload(data),
    }


@api.post("/history/{item_id}")
async def history_edit(item_id: int, req: HistoryEditReq, user: dict = Depends(current_user)):
    rec = get_history_by_id(item_id)
    if not rec:
        raise HTTPException(404, "Запись не найдена")
    if rec["user_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "Это чужая запись")

    data = json.loads(rec["data_json"])
    field = req.field

    if field in ("customs_eur", "util_rub", "customs_tks_rub", "evacuator_rub"):
        data[field] = float(req.value)

    elif field == "counterparty":
        data["counterparty"] = str(req.value)

    elif field == "buyback":
        # {"mode": "pct"|"fixed", "value": 12}
        if not isinstance(req.value, dict):
            raise HTTPException(400, "Выкуп передаётся объектом {mode, value}")
        mode = req.value.get("mode", "pct")
        try:
            val = float(req.value.get("value", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "Некорректная сумма выкупа")
        if mode not in ("pct", "fixed") or val <= 0:
            raise HTTPException(400, "Некорректный выкуп")
        apply_buyback(data, mode, val)

    elif field == "buyback_pct":
        # старый скалярный формат: ≤30 — процент, ≥100 — сумма в евро
        val = float(req.value)
        if val <= 30:
            apply_buyback(data, "pct", val)
        elif val >= 100:
            apply_buyback(data, "fixed", val)
        else:
            raise HTTPException(400, "Введите процент (1–30) или сумму в EUR (от 100)")

    else:
        raise HTTPException(400, f"Это поле править нельзя: {field}")

    update_history_data(item_id, data)

    sheet_error = ""
    if rec.get("sheet_row"):
        try:
            await asyncio.to_thread(update_car_row, rec["sheet_row"], data)
        except Exception as e:
            sheet_error = str(e)

    return {
        "rows":      card_rows(data),
        "total_rub": total_rub(data),
        "sheet_error": sheet_error,
    }


@api.post("/history/{item_id}/kp")
async def history_send_kp(item_id: int, req: PhotosReq, user: dict = Depends(current_user)):
    rec = get_history_by_id(item_id)
    if not rec:
        raise HTTPException(404, "Запись не найдена")
    data = json.loads(rec["data_json"])
    sent = await _send(user["id"], rec["car_num"], data, req.photos)
    if not sent:
        raise HTTPException(502, "Не удалось отправить КП в телеграм")
    return {"sent": True}


# ── Черновик выбора фото (переход из бота) ───────────────────────────────────

@api.get("/draft/{draft_id}")
async def draft_get(draft_id: str, user: dict = Depends(current_user)):
    rec = _load_draft(draft_id, user)
    d   = rec["data"]
    return {
        "stage":       "photos",
        "car":         _car_payload(d),
        "photos":      d.get("photos", []),
        "preselected": d.get("chosen_photos") or pick_photos(d.get("photos", [])),
        "car_num":     d.get("car_num"),
        "total_rub":   total_rub(d) if d.get("direction") else None,
    }


@api.post("/draft/{draft_id}/photos")
async def draft_photos(draft_id: str, req: PhotosReq, user: dict = Depends(current_user)):
    rec = _load_draft(draft_id, user)
    d   = rec["data"]
    if not req.photos:
        raise HTTPException(400, "Выберите хотя бы одно фото")

    sent = await _send(rec["chat_id"], d.get("car_num", "000"), d, req.photos)
    if not sent:
        raise HTTPException(502, "Не удалось отправить КП в телеграм")
    close_draft(draft_id)
    return {"sent": True}


# ── Настройки ────────────────────────────────────────────────────────────────

@api.get("/settings")
async def settings_get(user: dict = Depends(admin_user)):
    sections: dict[str, list] = {}
    for key, (label, unit, section) in EDITABLE_SETTINGS.items():
        sections.setdefault(section, []).append({
            "key": key, "label": label, "unit": unit, "value": get_setting(key),
        })
    return {
        "sections": [
            {"key": s, "title": SECTION_TITLES.get(s, s), "items": items}
            for s, items in sections.items()
        ],
        "rates": _rates_payload(),
    }


@api.post("/settings")
async def settings_set(req: SettingReq, user: dict = Depends(admin_user)):
    if req.key not in EDITABLE_SETTINGS:
        raise HTTPException(400, "Неизвестная настройка")

    value = req.value
    if req.key in ("img_count", "img_step", "img_offset"):
        value = max(0, int(value))
        if req.key in ("img_count", "img_step"):
            value = max(1, value)

    set_setting(req.key, str(value))

    # Правка курса руками = курс на сегодня
    if EDITABLE_SETTINGS[req.key][2] == "rates":
        set_setting("rates_date", _today())
        set_setting("rates_set_by", user["name"])

    return {"key": req.key, "value": str(value)}


@api.post("/rates")
async def rates_set(req: RatesReq, user: dict = Depends(admin_user)):
    r = get_rates()
    if r["rates_date"] == _today():
        who = f" ({r['rates_set_by']})" if r["rates_set_by"] else ""
        raise HTTPException(
            409,
            f"Курс на сегодня уже выставлен{who}: "
            f"EUR→USDT {r['rate_eur_usdt']}, USDT→₽ {r['rate_usdt_rub']}. Уточните позже.",
        )
    if req.rate_eur_usdt <= 0 or req.rate_usdt_rub <= 0:
        raise HTTPException(400, "Курс должен быть больше нуля")

    set_rates(req.rate_eur_usdt, req.rate_usdt_rub, _today(), user["name"])
    return _rates_payload()


# ── Приложение ───────────────────────────────────────────────────────────────

_bot_instance: Bot | None = None


def _bot() -> Bot | None:
    return _bot_instance


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_instance
    init_db()
    cleanup_drafts()
    if TOKEN:
        try:
            _bot_instance = Bot(TOKEN)
            await _bot_instance.initialize()
        except Exception as e:
            # Телеграм временно недоступен — API всё равно должен подняться
            _bot_instance = None
            print(f"WARNING: бот не инициализирован ({e}) — КП отправляться не будет")
    else:
        print("WARNING: TELEGRAM_BOT_TOKEN не задан — КП отправляться не будет")
    if DEV_MODE:
        print(f"DEV-режим: запросы без подписи проходят как пользователь {DEV_USER_ID}")
    yield
    if _bot_instance:
        await _bot_instance.shutdown()


class AppShellStatic(StaticFiles):
    """
    Статика мини-аппа с обязательной перепроверкой.

    Без заголовка Cache-Control браузер (и особенно WebView телеграма)
    решает срок жизни файла сам и может неделями отдавать старый calc.js —
    пользователь после деплоя видит прежний интерфейс. no-cache не запрещает
    кеш, а требует спросить сервер: если файл не менялся, ответ 304 и трафика
    почти нет.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="AutoKP Mini App", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api, prefix="/api")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    if FRONTEND_DIR.exists():
        app.mount("/", AppShellStatic(directory=str(FRONTEND_DIR), html=True), name="frontend")
    return app
