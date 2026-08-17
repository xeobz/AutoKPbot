"""
Telegram bot — car quote (КП) + Google Sheets logging.

Flow:
  link → scrape → counterparty → VAT → buyback% →
  customs → util → confirm → Sheets → history → КП
"""
import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from calc import (
    DIRECTION_LABELS,
    vat_from_percent,
    vat_percent,
    apply_buyback,
    buyback_label,
    buyback_options,
    card_rows,
    default_logistics,
    fmt_eur,
    fmt_rub,
    netto,
    total_rub,
)
from kpsend import CONTACT, send_kp
from scraper import find_listing_url, scrape
from storage import (
    EDITABLE_SETTINGS as _EDITABLE,
    close_draft,
    complete_pending,
    get_admin_ids,
    get_draft,
    save_draft,
    add_admin,
    remove_admin,
    get_all_admins,
    get_all_brand_emoji,
    get_brand_emoji,
    get_float,
    get_history_by_id,
    get_history_for_user,
    get_pending_by_id,
    get_pending_for_user,
    get_rates,
    get_setting,
    get_tariffs,
    init_db,
    remove_brand_emoji,
    save_history,
    save_pending,
    set_brand_emoji,
    set_rates,
    set_setting,
    update_history_data,
)
from sheets import append_car_row, append_kult40_row, append_msk_row, update_car_row

TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Seed admins from .env (always trusted); DB admins loaded after init_db()
_ENV_ADMIN_IDS: set[int] = {
    int(x.strip()) for x in os.getenv("ADMIN_TELEGRAM_ID", "").split(",")
    if x.strip().isdigit()
}
ADMIN_IDS: set[int] = set(_ENV_ADMIN_IDS)


def _reload_admin_ids() -> None:
    """Sync ADMIN_IDS from .env + database."""
    ADMIN_IDS.clear()
    ADMIN_IDS.update(_ENV_ADMIN_IDS)
    ADMIN_IDS.update(get_admin_ids())
CONTACT  = "@Aleksandr_Montaro"

# ── Conversation states ───────────────────────────────────────────────────────
(
    WAIT_URL,
    ASK_COUNTERPARTY,
    ASK_VAT,
    ASK_BUYBACK,
    ASK_CUSTOMS,
    ASK_UTIL,
    CONFIRM,
    SETTINGS_MENU,
    SETTINGS_AWAIT_VALUE,
    PENDING_CHOOSE,
    PENDING_CUSTOMS,
    PENDING_UTIL,
    HISTORY_LIST,
    HISTORY_ITEM,
    HISTORY_EDIT_VALUE,
    ADMIN_LIST,
    ADMIN_ADD_NAME,
    ADMIN_ADD_ID,
    KP_PHOTO_EDIT,
    ASK_DIRECTION,       # 19 — choose sheet (minsk / kult40 / msk)
    ASK_EVACUATOR,       # 20 — Эвакуатор СПБ-МСК (Культ40 only)
    ASK_CUSTOMS_TKS,     # 21 — Таможня ТКС (Культ40 + МСК)
    ASK_BUYBACK_MANUAL,  # 22 — выкуп: ввод суммы вручную
    RATES_AWAIT_EUR,     # 23 — курс дня: EUR→USDT
    RATES_AWAIT_RUB,     # 24 — курс дня: USDT→₽
    BRAND_AWAIT_NAME,    # 25 — эмодзи марок: название марки
    BRAND_AWAIT_EMOJI,   # 26 — эмодзи марок: сам премиум-эмодзи
    PHOTO_CHOICE,        # 27 — выбрать фото в мини-аппе или автоподбором
    ASK_VAT_MANUAL,      # 28 — НДС: свой процент
) = range(29)

# Адрес мини-аппа (https). Пусто — бот работает по-старому, без веба.
WEB_APP_URL = os.getenv("WEB_APP_URL", "").rstrip("/")

VAT_OPTIONS = [
    ("🇩🇪 19% (Германия)", "1.19"),
    ("🇧🇾 17% (Беларусь)", "1.17"),
    ("🇧🇪 21% (Бельгия)",  "1.21"),
    ("0% — без НДС",       "1.0"),
]
BUYBACK_PCTS = list(range(5, 16))   # 5–15 %

# Часовой пояс для утреннего опроса курсов — сервер живёт в UTC
MSK = timezone(timedelta(hours=3))
RATES_ASK_TIME = time(hour=8, minute=0, tzinfo=MSK)

# Keyboard button labels (used for routing)
_BTN_HISTORY  = "📋 История"
_BTN_PENDING  = "⏸ Незавершённые"
_BTN_SETTINGS = "⚙️ Настройки"

# Filter: all keyboard button texts (to exclude from text input handlers)
_KB_FILTER = filters.Regex(
    rf"^({re.escape(_BTN_HISTORY)}|{re.escape(_BTN_PENDING)}|{re.escape(_BTN_SETTINGS)})$"
)
_link_filter = filters.TEXT & ~filters.COMMAND & filters.Regex(r"mobile\.de|autoscout24\.")


# ── Keyboard helpers ──────────────────────────────────────────────────────────

def _main_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    row = [_BTN_HISTORY, _BTN_PENDING]
    if is_admin:
        row.append(_BTN_SETTINGS)
    return ReplyKeyboardMarkup(
        [row],
        resize_keyboard=True,
        input_field_placeholder="Вставьте ссылку mobile.de или autoscout24…",
    )


def _today() -> str:
    """Сегодняшняя дата по Москве — сервер живёт в UTC."""
    return datetime.now(MSK).strftime("%d.%m.%Y")


# ── Number parser (handles non-breaking spaces from mobile keyboards) ─────────

def _parse_number(text: str) -> float | None:
    cleaned = re.sub(r"[\s\xa0  ]+", "", text).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── Data helpers ──────────────────────────────────────────────────────────────

def _rates_for_calc() -> dict:
    """Курс дня — единый для всех направлений."""
    r = get_rates()
    return {
        "rate_eur_usdt": r["rate_eur_usdt"],
        "rate_usdt_rub": r["rate_usdt_rub"],
    }


def _build_card(d: dict, title: str = "📋 <b>Итоговая карточка</b>") -> str:
    """Карточка расчёта. Строки считает calc.py — те же, что видит веб."""
    head = [title, ""] if title else []
    head += [
        f"🚗 <b>{d.get('car_name', '—')}</b>",
        f"📍 Направление: {DIRECTION_LABELS.get(d.get('direction', 'minsk'), '—')}",
        f"👤 Контрагент: {d.get('counterparty', '—')}",
        f"🔗 <a href=\"{d.get('url','')}\">Объявление</a>",
        "",
    ]
    body = [
        f"{r['label']}: <b>{r['value']}</b>" if r["label"] else ""
        for r in card_rows(d)
    ]
    text = "\n".join(head + body)

    rates = get_rates()
    if not rates["rates_date"]:
        text += "\n\n⚠️ Курс дня ещё не задавался — считаю по сохранённому"
    elif rates["rates_date"] != _today():
        text += f"\n\n⚠️ Курс от {rates['rates_date']} — на сегодня не обновлён"
    return text


def _build_buyback_keyboard(h: float) -> InlineKeyboardMarkup:
    """
    Проценты выкупа. Все варианты ниже минималки схлопываются в одну кнопку,
    плюс кнопка ручного ввода суммы.
    """
    min_eur = get_float("buyback_min_eur", 2500)
    options = buyback_options(h)

    buttons, row = [], []
    for opt in options:
        if opt["below_min"]:
            continue                      # уходит в общую кнопку минималки
        row.append(InlineKeyboardButton(
            f"{opt['pct']}% — {fmt_eur(opt['eur'])}", callback_data=f"buyback:{opt['pct']}"
        ))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)

    # Кнопка минималки нужна, только если какие-то проценты в неё не проходят
    if any(o["below_min"] for o in options):
        buttons.insert(0, [InlineKeyboardButton(
            f"{fmt_eur(min_eur)} — минималка", callback_data="buyback:min"
        )])
    buttons.append([InlineKeyboardButton(
        "✏️ Ввести сумму", callback_data="buyback:manual"
    )])
    return InlineKeyboardMarkup(buttons)


def _history_edit_kb(direction: str = "minsk") -> InlineKeyboardMarkup:
    if direction == "kult40":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Эвакуатор ₽",   callback_data="hedit:evacuator_rub"),
             InlineKeyboardButton("✏️ Таможня ТКС ₽", callback_data="hedit:customs_tks_rub")],
            [InlineKeyboardButton("✏️ Выкуп %",       callback_data="hedit:buyback_pct"),
             InlineKeyboardButton("✏️ Контрагент",    callback_data="hedit:counterparty")],
            [InlineKeyboardButton("📄 Сгенерировать КП", callback_data="hedit:gen_kp")],
            [InlineKeyboardButton("⬅️ К списку",      callback_data="hist:back")],
        ])
    if direction == "msk":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Таможня ТКС ₽", callback_data="hedit:customs_tks_rub"),
             InlineKeyboardButton("✏️ Выкуп %",       callback_data="hedit:buyback_pct")],
            [InlineKeyboardButton("✏️ Контрагент",    callback_data="hedit:counterparty")],
            [InlineKeyboardButton("📄 Сгенерировать КП", callback_data="hedit:gen_kp")],
            [InlineKeyboardButton("⬅️ К списку",      callback_data="hist:back")],
        ])
    # minsk (default)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Таможня EUR",  callback_data="hedit:customs_eur"),
         InlineKeyboardButton("✏️ Утиль ₽",     callback_data="hedit:util_rub")],
        [InlineKeyboardButton("✏️ Выкуп %",     callback_data="hedit:buyback_pct"),
         InlineKeyboardButton("✏️ Контрагент",  callback_data="hedit:counterparty")],
        [InlineKeyboardButton("📄 Сгенерировать КП", callback_data="hedit:gen_kp")],
        [InlineKeyboardButton("⬅️ К списку",    callback_data="hist:back")],
    ])


# ── KP generation + sending ───────────────────────────────────────────────────

async def _send_kp(chat_id: int, car_num: int, d: dict, bot) -> dict | None:
    """Отправка КП. Логика общая с веб-API — лежит в kpsend.py."""
    return await send_kp(bot, chat_id, car_num, d)


# ── Выбор фото: мини-апп или автоподбор ──────────────────────────────────────

def _photo_choice_kb(draft_id: str) -> InlineKeyboardMarkup:
    rows = []
    if WEB_APP_URL:
        rows.append([InlineKeyboardButton(
            "🖼 Выбрать фото",
            web_app=WebAppInfo(url=f"{WEB_APP_URL}/?draft={draft_id}"),
        )])
    rows.append([InlineKeyboardButton(
        "⚡ Отправить с автоподбором", callback_data=f"autokp:{draft_id}"
    )])
    rows.append([InlineKeyboardButton(
        "📋 Без КП — только запись", callback_data=f"nokp:{draft_id}"
    )])
    return InlineKeyboardMarkup(rows)


async def _offer_photo_choice(chat_id: int, user_id: int, car_num: int, d: dict, ctx) -> int:
    """
    После записи в таблицу предлагаем выбрать фото:
    кнопка открывает мини-апп на нужном черновике, либо автоподбор прямо тут.
    Если адрес мини-аппа не задан — сразу шлём КП автоподбором.
    """
    data = dict(d)
    data["car_num"] = car_num

    if not WEB_APP_URL:
        kp = await _send_kp(chat_id, car_num, data, ctx.bot)
        if kp:
            ctx.user_data["_kp_edit"] = kp
            await ctx.bot.send_message(
                chat_id=chat_id,
                text="Если нужно заменить фото в КП:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Изменить фото", callback_data="kpedit:start"),
                    InlineKeyboardButton("✅ Готово",         callback_data="kpedit:done"),
                ]]),
            )
            return KP_PHOTO_EDIT
        return WAIT_URL

    draft_id = uuid.uuid4().hex[:12]
    save_draft(draft_id, user_id, chat_id, data)
    await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            "🖼 <b>Фото для КП</b>\n\n"
            "Откройте приложение и отметьте нужные кадры — "
            "или отправлю подборку сам."
        ),
        parse_mode="HTML",
        reply_markup=_photo_choice_kb(draft_id),
    )
    return PHOTO_CHOICE


async def photo_choice_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Отправить с автоподбором» — КП уходит без захода в мини-апп."""
    query = update.callback_query
    await query.answer()
    draft_id = query.data.split(":")[1]

    rec = get_draft(draft_id)
    if not rec:
        await query.edit_message_text("❌ Черновик не найден — начните расчёт заново.")
        return WAIT_URL

    d = rec["data"]
    await query.edit_message_text("⏳ Генерирую КП…")
    kp = await _send_kp(rec["chat_id"], d.get("car_num", "000"), d, ctx.bot)
    close_draft(draft_id)

    if kp:
        ctx.user_data["_kp_edit"] = kp
        await ctx.bot.send_message(
            chat_id=rec["chat_id"],
            text="Если нужно заменить фото в КП:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✏️ Изменить фото", callback_data="kpedit:start"),
                InlineKeyboardButton("✅ Готово",         callback_data="kpedit:done"),
            ]]),
        )
        return KP_PHOTO_EDIT
    return WAIT_URL


async def photo_choice_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Без КП» — строка в таблице уже записана, текст клиенту не нужен."""
    query = update.callback_query
    await query.answer()
    draft_id = query.data.split(":")[1]
    close_draft(draft_id)
    await query.edit_message_text(
        "✅ Записано в таблицу, без КП.\n"
        "Если понадобится — КП можно отправить позже из «📋 История».",
    )
    return WAIT_URL


async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """/app — открыть мини-апп."""
    if not WEB_APP_URL:
        await update.message.reply_text("Мини-апп пока не подключён.")
        return WAIT_URL
    await update.message.reply_text(
        "🚀 Открыть приложение:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Открыть", web_app=WebAppInfo(url=f"{WEB_APP_URL}/"))
        ]]),
    )
    return WAIT_URL


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user     = update.effective_user
    is_admin = user.id in ADMIN_IDS
    await update.message.reply_text(
        "👋 Привет! Отправь ссылку с <b>mobile.de</b> или <b>autoscout24</b> — начнём расчёт.\n\n"
        "Команды:\n"
        "/app — открыть приложение\n"
        "/pending — незавершённые запросы\n"
        "/rates — курс дня (для администратора)\n"
        "/settings — курсы, тарифы, фото (для администратора)",
        parse_mode="HTML",
        reply_markup=_main_kb(is_admin),
    )
    return WAIT_URL


# ── Step 1: receive URL ───────────────────────────────────────────────────────

async def receive_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    found = find_listing_url(update.message.text or "")
    if not found:
        await update.message.reply_text(
            "❌ Не нашёл ссылку. Пришли ссылку с <b>mobile.de</b> или <b>autoscout24</b>.",
            parse_mode="HTML",
            reply_markup=_main_kb(update.effective_user.id in ADMIN_IDS),
        )
        return WAIT_URL
    url, source = found

    # ── Photo debug mode (🖼 Отладка фото in settings) ───────────────────────
    if ctx.user_data.pop("_img_debug_mode", False):
        wait_msg = await update.message.reply_text("⏳ Загружаю объявление…")
        try:
            result = await scrape(url)
        except Exception as e:
            await wait_msg.edit_text(f"❌ Ошибка загрузки:\n<code>{e}</code>", parse_mode="HTML")
            return WAIT_URL

        gallery = result.get("photos", [])       # smart cluster
        all_imgs = result.get("all_photos", [])  # everything flat

        await wait_msg.delete()

        # ── Section 1: smart gallery cluster ──────────────────────────────
        await update.message.reply_text(
            f"🎯 <b>Умная галерея</b> (кластер): <b>{len(gallery)}</b> фото\n"
            f"<i>Именно эти идут в КП</i>",
            parse_mode="HTML",
        )
        for i, photo_url in enumerate(gallery):
            try:
                await update.message.reply_photo(photo=photo_url, caption=f"gallery #{i}")
            except Exception:
                await update.message.reply_text(f"gallery #{i} — ❌ не загрузилось")

        # ── Section 2: все остальные (не вошедшие в кластер) ─────────────
        outside = [u for u in all_imgs if u not in gallery]
        if outside:
            await update.message.reply_text(
                f"🗑 <b>Остальные</b> (вне галереи): <b>{len(outside)}</b> фото",
                parse_mode="HTML",
            )
            for i, photo_url in enumerate(outside):
                try:
                    await update.message.reply_photo(photo=photo_url, caption=f"other #{i}")
                except Exception:
                    await update.message.reply_text(f"other #{i} — ❌ не загрузилось")

        await update.message.reply_text(
            "✅ Готово. Если галерея правильная — всё работает.\n"
            "Если нет — скажи что не так."
        )
        return WAIT_URL

    # Guard: prevent duplicate concurrent scraping
    if ctx.user_data.get("_scraping"):
        await update.message.reply_text("⏳ Уже загружаю объявление, подожди…")
        return WAIT_URL

    ctx.user_data.clear()
    ctx.user_data["url"] = url
    ctx.user_data["source"] = source

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🏙 ЕС/Минск",  callback_data="dir:minsk"),
        InlineKeyboardButton("🏭 Культ40",   callback_data="dir:kult40"),
        InlineKeyboardButton("🌆 ЕС-МСК",    callback_data="dir:msk"),
    ]])
    await update.message.reply_text(
        "Выберите <b>направление</b>:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return ASK_DIRECTION


# ── Step 1b: direction chosen → scrape ───────────────────────────────────────

async def receive_direction(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query     = update.callback_query
    await query.answer()
    direction = query.data.split(":")[1]   # "minsk" / "kult40" / "msk"
    ctx.user_data["direction"] = direction

    # Курс дня один на все направления, логистика — своя для каждого листа
    tf = get_tariffs()
    ctx.user_data.update(_rates_for_calc())
    ctx.user_data["logistics"] = default_logistics(direction, tf)
    ctx.user_data["epts_rub"]  = tf["epts_rub"]
    # Снимок тарифов: чтобы запись в истории не пересчиталась после их правки
    ctx.user_data["tariffs"]   = tf

    url = ctx.user_data.get("url", "")
    ctx.user_data["_scraping"] = True
    await query.edit_message_text("⏳ Загружаю объявление…")

    try:
        scraped = await scrape(url)
    except Exception as e:
        ctx.user_data.pop("_scraping", None)
        await query.edit_message_text(f"❌ Ошибка загрузки: {e}\n\nПопробуй ещё раз.")
        return WAIT_URL
    finally:
        ctx.user_data.pop("_scraping", None)

    make     = scraped.get("make", "")
    model    = scraped.get("model", "")
    year     = scraped.get("year", "")
    price    = scraped.get("price_eur") or 0.0
    title    = scraped.get("title") or f"{make} {model}".strip()
    car_name = title or f"{make} {model} {year}".strip()

    ctx.user_data.update({
        "car_name":  car_name,
        "title":     title,
        "price_eur": price,
        "make":      make,
        "model":     model,
        "year":      year,
        "mileage":   scraped.get("mileage"),
        "color":     scraped.get("color", ""),
        "features":  scraped.get("features", []),
        "photos":    scraped.get("photos", []),
        "all_photos": scraped.get("all_photos", []),
        "power_hp":  scraped.get("power_hp"),
        "engine_l":  scraped.get("engine_l"),
        "fuel":      scraped.get("fuel", ""),
        "gearbox":   scraped.get("gearbox", ""),
    })

    dir_label = {"minsk": "🏙 ЕС/Минск", "kult40": "🏭 ЕС/Культ40", "msk": "🌆 ЕС-МСК"}.get(direction, direction)
    await query.edit_message_text(
        f"✅ Нашёл: <b>{car_name}</b>  ·  {dir_label}\n"
        f"💶 Цена: <b>{fmt_eur(price)}</b>\n\n"
        f"Введите имя <b>контрагента</b>:",
        parse_mode="HTML",
    )
    return ASK_COUNTERPARTY


# ── Step 2: counterparty ──────────────────────────────────────────────────────

async def receive_counterparty(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user         = update.effective_user
    manager      = user.first_name or user.username or "Менеджер"
    counterparty = (update.message.text or "").strip()
    if not counterparty:
        await update.message.reply_text("Введите имя контрагента:")
        return ASK_COUNTERPARTY

    ctx.user_data["counterparty"] = f"{counterparty}({manager})"

    rows = [
        [InlineKeyboardButton(label, callback_data=f"vat:{val}")]
        for label, val in VAT_OPTIONS
    ]
    rows.append([InlineKeyboardButton("✏️ Свой процент", callback_data="vat:custom")])
    kb = InlineKeyboardMarkup(rows)
    await update.message.reply_text(
        f"👤 Контрагент: <b>{counterparty}({manager})</b>\n\n"
        "Выберите <b>НДС</b> страны продавца:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return ASK_VAT


# ── Step 3: VAT ───────────────────────────────────────────────────────────────

async def _after_vat(update_or_query, ctx: ContextTypes.DEFAULT_TYPE, vat: float) -> int:
    """Ставим НДС и переходим к выкупу — общий хвост для кнопок и ручного ввода."""
    ctx.user_data["vat"] = vat
    h = ctx.user_data["price_eur"] / vat

    text = (
        f"✅ НДС: <b>{vat_percent(vat)}</b> → НЕТТО: <b>{fmt_eur(h)}</b>\n\n"
        "Выберите <b>процент выкупа</b>:"
    )
    kb = _build_buyback_keyboard(h)
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update_or_query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return ASK_BUYBACK


async def receive_vat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "custom":
        await query.edit_message_text(
            "Введите <b>процент НДС</b> числом.\n"
            "<i>Например: 23. Если НДС нет — 0.</i>",
            parse_mode="HTML",
        )
        return ASK_VAT_MANUAL

    return await _after_vat(query, ctx, float(choice))


async def receive_vat_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_VAT_MANUAL — свой процент НДС."""
    raw = (update.message.text or "").strip().rstrip("%").strip()
    val = _parse_number(raw)
    if val is None or val < 0 or val > 100:
        await update.message.reply_text("Введите процент от 0 до 100, например: 23")
        return ASK_VAT_MANUAL

    # Кто-то по привычке впишет коэффициент (1.19) — такого НДС не бывает,
    # поэтому значения 1.01–1.5 с запятой понимаем как коэффициент
    if 1 < val < 1.5 and ("." in raw or "," in raw):
        vat = val
    else:
        vat = vat_from_percent(val)

    return await _after_vat(update, ctx, round(vat, 4))


# ── Step 4: buyback % ─────────────────────────────────────────────────────────

async def _after_buyback(update_or_query, ctx: ContextTypes.DEFAULT_TYPE, label: str, k: float) -> int:
    """Следующий шаг после выбора выкупа — свой для каждого направления."""
    direction = ctx.user_data.get("direction", "minsk")
    is_query  = hasattr(update_or_query, "edit_message_text")

    async def reply(text: str, **kw):
        if is_query:
            return await update_or_query.edit_message_text(text, **kw)
        return await update_or_query.message.reply_text(text, **kw)

    if direction == "kult40":
        await reply(
            f"✅ Выкуп <b>{label}</b> → <b>{fmt_eur(k)}</b>\n\n"
            f"Введите <b>стоимость эвакуатора СПБ-МСК</b> (руб.):",
            parse_mode="HTML",
        )
        return ASK_EVACUATOR

    if direction == "msk":
        await reply(
            f"✅ Выкуп <b>{label}</b> → <b>{fmt_eur(k)}</b>\n\n"
            f"Введите <b>таможню ТКС</b> (руб.):",
            parse_mode="HTML",
        )
        return ASK_CUSTOMS_TKS

    # minsk — исходный флоу
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏸ Отложить — введу позже", callback_data="customs:defer")
    ]])
    await reply(
        f"✅ Выкуп <b>{label}</b> → K = <b>{fmt_eur(k)}</b>\n\n"
        f"Введите <b>таможню РБ</b> в EUR\n(или нажмите «Отложить»):",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return ASK_CUSTOMS


async def receive_buyback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    g   = ctx.user_data["price_eur"]
    vat = ctx.user_data["vat"]
    h   = g / vat

    # ── Ввод суммы вручную ────────────────────────────────────────────────────
    if choice == "manual":
        await query.edit_message_text(
            f"НЕТТО: <b>{fmt_eur(h)}</b>\n\n"
            f"Введите <b>сумму выкупа</b> в EUR (например: 3200):",
            parse_mode="HTML",
        )
        return ASK_BUYBACK_MANUAL

    # ── Минималка ─────────────────────────────────────────────────────────────
    if choice == "min":
        k = get_float("buyback_min_eur", 2500)
        apply_buyback(ctx.user_data, "fixed", k)
        return await _after_buyback(query, ctx, "минималка", k)

    # ── Процент ───────────────────────────────────────────────────────────────
    pct = int(choice)
    apply_buyback(ctx.user_data, "pct", pct)
    return await _after_buyback(query, ctx, f"{pct}%", ctx.user_data["buyback_val"])


async def receive_buyback_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_BUYBACK_MANUAL — сумма выкупа, введённая руками."""
    val = _parse_number((update.message.text or "").strip())
    if val is None or val <= 0:
        await update.message.reply_text("Введите сумму в EUR, например: 3200")
        return ASK_BUYBACK_MANUAL

    apply_buyback(ctx.user_data, "fixed", val)
    return await _after_buyback(update, ctx, "вручную", val)


# ── Steps for ЕС/Культ40 and ЕС-МСК ─────────────────────────────────────────

async def receive_evacuator(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_EVACUATOR — Эвакуатор СПБ-МСК (Культ40 only), then go to customs TKS."""
    val = _parse_number((update.message.text or "").strip())
    if val is None:
        await update.message.reply_text("Введите число (руб.), например: 40000")
        return ASK_EVACUATOR

    ctx.user_data["evacuator_rub"] = val
    await update.message.reply_text(
        f"✅ Эвакуатор: <b>{fmt_rub(val)}</b>\n\n"
        f"Введите <b>таможню ТКС</b> (руб.):",
        parse_mode="HTML",
    )
    return ASK_CUSTOMS_TKS


async def receive_customs_tks(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """ASK_CUSTOMS_TKS — Таможня ТКС for Культ40 and МСК, then confirm."""
    val = _parse_number((update.message.text or "").strip())
    if val is None:
        await update.message.reply_text("Введите число (руб.), например: 500000")
        return ASK_CUSTOMS_TKS

    ctx.user_data["customs_tks_rub"] = val
    d  = ctx.user_data
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Записать в таблицу", callback_data="confirm:yes"),
        InlineKeyboardButton("✏️ Начать заново",      callback_data="confirm:no"),
    ]])
    await update.message.reply_text(
        _build_card(d),
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return CONFIRM


# ── Step 5: customs ───────────────────────────────────────────────────────────

async def receive_customs_defer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    pid = save_pending(
        user_id=user.id,
        chat_id=update.effective_chat.id,
        manager_name=user.first_name or "",
        car_label=ctx.user_data.get("car_name", ""),
        data=dict(ctx.user_data),
    )
    await query.edit_message_text(
        f"⏸ Запрос сохранён (#{pid}).\n"
        f"🚗 {ctx.user_data.get('car_name')}\n\n"
        "Используйте /pending или кнопку «⏸ Незавершённые» чтобы завершить.",
    )
    ctx.user_data.clear()
    return WAIT_URL


async def receive_customs_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    customs = _parse_number((update.message.text or "").strip())
    if customs is None:
        await update.message.reply_text("Введите число (EUR), например: 8500")
        return ASK_CUSTOMS

    ctx.user_data["customs_eur"] = customs
    await update.message.reply_text(
        f"✅ Таможня: <b>{fmt_eur(customs)}</b>\n\nВведите <b>утиль</b> (руб.):",
        parse_mode="HTML",
    )
    return ASK_UTIL


# ── Step 6: util ─────────────────────────────────────────────────────────────

async def receive_util(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    util = _parse_number((update.message.text or "").strip())
    if util is None:
        await update.message.reply_text("Введите число (руб.), например: 4500000")
        return ASK_UTIL

    ctx.user_data["util_rub"] = util
    d  = ctx.user_data
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Записать в таблицу", callback_data="confirm:yes"),
        InlineKeyboardButton("✏️ Начать заново",      callback_data="confirm:no"),
    ]])
    await update.message.reply_text(
        _build_card(d),
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return CONFIRM


# ── Step 7: confirm → write to Sheets ────────────────────────────────────────

async def receive_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query  = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "no":
        await query.edit_message_text("↩️ Начни заново — отправь новую ссылку.")
        ctx.user_data.clear()
        return WAIT_URL

    await query.edit_message_text("⏳ Записываем в таблицу…")

    direction = ctx.user_data.get("direction", "minsk")
    _writers  = {"kult40": append_kult40_row, "msk": append_msk_row}
    writer    = _writers.get(direction, append_car_row)

    try:
        car_num, sheet_row = writer(ctx.user_data)
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка записи в таблицу:\n{e}")
        return WAIT_URL

    # Save to local history
    user = update.effective_user
    d    = ctx.user_data
    save_history(
        user_id=user.id,
        chat_id=query.message.chat_id,
        car_num=car_num,
        sheet_row=sheet_row,
        car_name=d.get("car_name", ""),
        counterparty=d.get("counterparty", ""),
        url=d.get("url", ""),
        data=dict(d),
    )

    await query.edit_message_text(
        f"✅ <b>Записано #{car_num}</b>\n"
        f"🚗 {d.get('car_name')}\n"
        f"👤 {d.get('counterparty')}",
        parse_mode="HTML",
    )

    chat_id       = query.message.chat_id
    data_snapshot = dict(d)
    ctx.user_data.clear()

    return await _offer_photo_choice(chat_id, user.id, car_num, data_snapshot, ctx)


# ── History ───────────────────────────────────────────────────────────────────

async def show_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user  = update.effective_user
    items = get_history_for_user(user.id)

    if not items:
        text = "📋 <b>История пуста</b>\n\nОтправьте ссылку mobile.de для нового расчёта."
        kb   = None
    else:
        text = f"📋 <b>История запросов</b> (последние {len(items)}):"
        rows = []
        for it in items:
            date  = it["created_at"][:10]
            label = f"🚗 {it['car_name'][:22]}  ·  {date}"
            rows.append([InlineKeyboardButton(label, callback_data=f"hist:{it['id']}")])
        kb = InlineKeyboardMarkup(rows)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

    return HISTORY_LIST


async def history_open_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hist_id = int(query.data.split(":")[1])

    record = get_history_by_id(hist_id)
    if not record:
        await query.edit_message_text("❌ Запись не найдена.")
        return HISTORY_LIST

    data = json.loads(record["data_json"])
    ctx.user_data["_hist_id"]        = hist_id
    ctx.user_data["_hist_sheet_row"] = record.get("sheet_row")
    ctx.user_data["_hist_car_num"]   = record.get("car_num")
    ctx.user_data["_hist_data"]      = data

    header = (
        f"📋 <b>Запись #{record['car_num']}</b>  ·  {record['created_at'][:10]}\n"
    )
    await query.edit_message_text(
        header + _build_card(data, title=""),
        reply_markup=_history_edit_kb(data.get("direction", "minsk")),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return HISTORY_ITEM


async def history_edit_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle edit-field button or gen_kp / back from history item view."""
    query  = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "back":
        return await show_history(update, ctx)

    if action == "gen_kp":
        hist_id = ctx.user_data.get("_hist_id")
        record  = get_history_by_id(hist_id) if hist_id else None
        if not record:
            await query.edit_message_text("❌ Запись не найдена.")
            return HISTORY_LIST
        data    = json.loads(record["data_json"])
        car_num = record.get("car_num", 0)
        await query.edit_message_text("⏳ Генерирую КП…")
        kp_chat = query.message.chat_id
        kp_result = await _send_kp(kp_chat, car_num, data, ctx.bot)
        if kp_result:
            ctx.user_data["_kp_edit"] = kp_result
            await ctx.bot.send_message(
                chat_id=kp_chat,
                text="Если нужно заменить фото в КП:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Изменить фото", callback_data="kpedit:start"),
                    InlineKeyboardButton("✅ Готово",         callback_data="kpedit:done"),
                ]]),
            )
            return KP_PHOTO_EDIT
        return HISTORY_LIST

    if action == "cancel":
        # Return to the history item card
        hist_id = ctx.user_data.get("_hist_id")
        record  = get_history_by_id(hist_id) if hist_id else None
        if not record:
            await query.edit_message_text("❌ Запись не найдена.")
            return HISTORY_LIST
        data    = json.loads(record["data_json"])
        car_num = record.get("car_num")
        header  = f"📋 <b>Запись #{car_num}</b>  ·  {record['created_at'][:10]}\n"
        await query.edit_message_text(
            header + _build_card(data, title=""),
            reply_markup=_history_edit_kb(data.get("direction", "minsk")),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return HISTORY_ITEM

    # Entering edit mode for a field
    labels = {
        "customs_eur":     "таможню РБ в EUR (например: 18500)",
        "util_rub":        "утиль в рублях (например: 1 850 000)",
        "evacuator_rub":   "эвакуатор СПБ-МСК в рублях (например: 40000)",
        "customs_tks_rub": "таможню ТКС в рублях (например: 500000)",
        "buyback_pct":     "процент выкупа (1–30) или сумму в EUR (от 100)",
        "counterparty":    "имя контрагента",
    }
    ctx.user_data["_hist_edit_field"] = action
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data="hedit:cancel")
    ]])
    await query.edit_message_text(
        f"✏️ Введите новое значение\n<b>{labels.get(action, action)}</b>:",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    return HISTORY_EDIT_VALUE


async def history_edit_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive new value, update history record and Google Sheet."""
    field      = ctx.user_data.get("_hist_edit_field")
    hist_id    = ctx.user_data.get("_hist_id")
    sheet_row  = ctx.user_data.get("_hist_sheet_row")
    car_num    = ctx.user_data.get("_hist_car_num")

    if not field or not hist_id:
        await update.message.reply_text("❌ Ошибка состояния. Вернись к истории.")
        return WAIT_URL

    record = get_history_by_id(hist_id)
    if not record:
        await update.message.reply_text("❌ Запись не найдена.")
        return WAIT_URL

    data = json.loads(record["data_json"])
    text = (update.message.text or "").strip()

    # Validate & apply
    if field in ("customs_eur", "util_rub", "evacuator_rub", "customs_tks_rub"):
        val = _parse_number(text)
        if val is None:
            await update.message.reply_text("Введите число:")
            return HISTORY_EDIT_VALUE
        data[field] = val

    elif field == "buyback_pct":
        val = _parse_number(text)
        if val is None or val <= 0:
            await update.message.reply_text("Введите процент (1–30) или сумму в EUR (от 100):")
            return HISTORY_EDIT_VALUE
        if val <= 30:                     # процент
            apply_buyback(data, "pct", int(val))
        elif val >= 100:                  # фиксированная сумма в EUR
            apply_buyback(data, "fixed", val)
        else:
            await update.message.reply_text("Введите процент (1–30) или сумму в EUR (от 100):")
            return HISTORY_EDIT_VALUE

    elif field == "counterparty":
        data["counterparty"] = text

    else:
        await update.message.reply_text("Неизвестное поле.")
        return HISTORY_ITEM

    # Persist to DB
    update_history_data(hist_id, data)
    ctx.user_data["_hist_data"] = data

    # Update Google Sheet row
    sheet_err = ""
    if sheet_row:
        try:
            update_car_row(sheet_row, data)
        except Exception as e:
            sheet_err = f"\n⚠️ Таблица не обновлена: {e}"

    header = f"📋 <b>Запись #{car_num}</b>  ·  {record['created_at'][:10]}\n"
    await update.message.reply_text(
        f"✅ Обновлено!{sheet_err}\n\n" + header + _build_card(data, title=""),
        reply_markup=_history_edit_kb(data.get("direction", "minsk")),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return HISTORY_ITEM


# ── /pending ──────────────────────────────────────────────────────────────────

async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user  = update.effective_user
    items = get_pending_for_user(user.id)

    if not items:
        msg = "✅ Нет незавершённых запросов."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return WAIT_URL

    buttons = [
        [InlineKeyboardButton(
            f"#{p['id']} {p['car_label']} — {p['created_at'][:10]}",
            callback_data=f"pending_pick:{p['id']}"
        )]
        for p in items
    ]
    text = "⏸ <b>Незавершённые запросы</b>\nВыберите для завершения:"
    kb   = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

    return PENDING_CHOOSE


async def pending_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    pid   = int(query.data.split(":")[1])

    record = get_pending_by_id(pid)
    if not record:
        await query.edit_message_text("❌ Запрос не найден.")
        return WAIT_URL

    data = json.loads(record["data_json"])
    ctx.user_data.update(data)
    ctx.user_data["_pending_id"] = pid

    await query.edit_message_text(
        f"🚗 <b>{record['car_label']}</b>\n\nВведите <b>таможню РБ</b> в EUR:",
        parse_mode="HTML",
    )
    return PENDING_CUSTOMS


async def pending_customs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    customs = _parse_number((update.message.text or "").strip())
    if customs is None:
        await update.message.reply_text("Введите число (EUR):")
        return PENDING_CUSTOMS

    ctx.user_data["customs_eur"] = customs
    await update.message.reply_text(
        f"✅ Таможня: {fmt_eur(customs)}\n\nВведите <b>утиль</b> (руб.):",
        parse_mode="HTML",
    )
    return PENDING_UTIL


async def pending_util(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    util = _parse_number((update.message.text or "").strip())
    if util is None:
        await update.message.reply_text("Введите число (руб.):")
        return PENDING_UTIL

    ctx.user_data["util_rub"] = util

    try:
        car_num, sheet_row = append_car_row(ctx.user_data)
        pid = ctx.user_data.pop("_pending_id", None)
        if pid:
            complete_pending(pid)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка записи: {e}")
        return WAIT_URL

    user = update.effective_user
    d    = ctx.user_data
    save_history(
        user_id=user.id,
        chat_id=update.effective_chat.id,
        car_num=car_num,
        sheet_row=sheet_row,
        car_name=d.get("car_name", ""),
        counterparty=d.get("counterparty", ""),
        url=d.get("url", ""),
        data=dict(d),
    )

    await update.message.reply_text(
        f"✅ <b>Записано #{car_num}</b>\n🚗 {d.get('car_name')}",
        parse_mode="HTML",
    )

    chat_id       = update.effective_chat.id
    data_snapshot = dict(d)
    ctx.user_data.clear()

    return await _offer_photo_choice(chat_id, user.id, car_num, data_snapshot, ctx)


# ── KP photo editing ──────────────────────────────────────────────────────────

def _kp_selection_kb(n: int, selected: set[int], remaining: int) -> InlineKeyboardMarkup:
    """Build photo-selection keyboard with toggle buttons and action row."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i in range(n):
        label = f"✅ {i + 1}" if i in selected else str(i + 1)
        row.append(InlineKeyboardButton(label, callback_data=f"kpedit:sel:{i}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    sel_count = len(selected)
    apply_label = (
        f"🔄 Заменить ({sel_count})" if sel_count else "🔄 Заменить выбранные"
    )
    rows.append([
        InlineKeyboardButton(apply_label, callback_data="kpedit:apply"),
        InlineKeyboardButton("✅ Готово",  callback_data="kpedit:done"),
    ])
    return InlineKeyboardMarkup(rows)


def _kp_selection_text(selected: set[int], remaining: int) -> str:
    hint = "Нажми на номер — отметь ✅, нажми снова — снимет отметку." if not selected else \
           f"Отмечено: {', '.join(str(i+1) for i in sorted(selected))}"
    return (
        f"Выберите фото для замены:\n"
        f"{hint}\n"
        f"<i>Доступно новых фото в галерее: {remaining}</i>"
    )


async def kp_edit_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle all kpedit: callbacks — toggle, apply, done."""
    query = update.callback_query
    await query.answer()
    parts  = query.data.split(":")
    action = parts[1]

    kp = ctx.user_data.get("_kp_edit")

    if action == "done":
        ctx.user_data.pop("_kp_edit", None)
        await query.edit_message_text("✅ КП готов.")
        return WAIT_URL

    if not kp:
        # Набор фото хранится в памяти и не переживает перезапуск бота
        await query.edit_message_text(
            "⌛ Это КП уже не редактируется — бот перезапускался.\n"
            "Откройте «📋 История» и отправьте КП заново."
        )
        return WAIT_URL

    used    = kp["used_photos"]
    shown   = kp["shown_photos"]
    all_p   = kp["all_photos"]
    msg_ids = kp["photo_msg_ids"]
    chat_id = kp["chat_id"]
    selected: set[int] = kp.setdefault("_selected", set())

    # ── Show selection keyboard ───────────────────────────────────────────────
    if action == "start":
        selected.clear()
        remaining = len([u for u in all_p if u not in shown])
        await query.edit_message_text(
            _kp_selection_text(selected, remaining),
            parse_mode="HTML",
            reply_markup=_kp_selection_kb(len(msg_ids), selected, remaining),
        )
        return KP_PHOTO_EDIT

    # ── Toggle one photo ──────────────────────────────────────────────────────
    if action == "sel":
        idx = int(parts[2])
        if idx in selected:
            selected.discard(idx)
        else:
            selected.add(idx)
        remaining = len([u for u in all_p if u not in shown])
        await query.edit_message_text(
            _kp_selection_text(selected, remaining),
            parse_mode="HTML",
            reply_markup=_kp_selection_kb(len(msg_ids), selected, remaining),
        )
        return KP_PHOTO_EDIT

    # ── Apply replacements ────────────────────────────────────────────────────
    if action == "apply":
        if not selected:
            await query.answer("Выберите хотя бы одно фото!", show_alert=True)
            return KP_PHOTO_EDIT

        caption     = kp.get("caption")
        caption_idx = kp.get("caption_idx", 0)

        replaced = 0
        for idx in sorted(selected):
            pool = [u for u in all_p if u not in shown]
            if not pool:
                break
            new_url = pool[0]
            # If replacing the photo that carries the caption, re-attach it
            if caption and idx == caption_idx:
                new_media = InputMediaPhoto(media=new_url, caption=caption, parse_mode="HTML")
            else:
                new_media = InputMediaPhoto(media=new_url)
            try:
                await ctx.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=msg_ids[idx],
                    media=new_media,
                )
                used[idx] = new_url
                shown.add(new_url)
                replaced += 1
            except Exception:
                pass

        selected.clear()
        remaining = len([u for u in all_p if u not in shown])

        status = f"✅ Заменено {replaced} фото." if replaced else "😕 Ничего не заменено."
        await query.edit_message_text(
            f"{status}\n\n" + _kp_selection_text(selected, remaining),
            parse_mode="HTML",
            reply_markup=_kp_selection_kb(len(msg_ids), selected, remaining),
        )
        return KP_PHOTO_EDIT

    return KP_PHOTO_EDIT


# ── /settings (admin only) ────────────────────────────────────────────────────

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        msg = "⛔ Только для администратора."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return WAIT_URL
    return await _show_settings_menu(update, ctx)


def _fmt_setting(key: str) -> str:
    label, unit, _ = _EDITABLE[key]
    val = get_setting(key)
    try:
        num = float(val)
        val = f"{num:,.0f}".replace(",", " ") if num >= 1000 else f"{num:g}"
    except (TypeError, ValueError):
        pass
    return f"{label}: <b>{val}{(' ' + unit) if unit else ''}</b>"


async def _reply(update_or_query, text: str, kb: InlineKeyboardMarkup) -> None:
    """Отправка/редактирование — работает и с Update, и с CallbackQuery."""
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    elif getattr(update_or_query, "callback_query", None):
        await update_or_query.callback_query.edit_message_text(
            text, reply_markup=kb, parse_mode="HTML"
        )
    else:
        await update_or_query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def _show_settings_menu(update_or_query, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    r = get_rates()
    when = r["rates_date"] or "не задавался"
    who  = f" · {r['rates_set_by']}" if r["rates_set_by"] else ""
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"💱 <b>Курс дня</b> ({when}{who})\n"
        f"  EUR→USDT: <b>{r['rate_eur_usdt']}</b>\n"
        f"  USDT→₽: <b>{r['rate_usdt_rub']}</b>\n\n"
        "Выберите раздел:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💱 Изменить курс дня", callback_data="set:rates")],
        [InlineKeyboardButton("📦 Тарифы",            callback_data="set:tariffs")],
        [InlineKeyboardButton("🖼 Фото",              callback_data="set:photo")],
        [InlineKeyboardButton("😀 Эмодзи марок",      callback_data="set:brands")],
        [InlineKeyboardButton("👥 Администраторы",    callback_data="set:admins")],
        [InlineKeyboardButton("🔍 Отладка фото",      callback_data="set:img_debug")],
        [InlineKeyboardButton("❌ Закрыть",           callback_data="set:close")],
    ])
    await _reply(update_or_query, text, kb)
    return SETTINGS_MENU


async def _show_section(update_or_query, section: str) -> int:
    """Раздел настроек: список значений + кнопка на каждое."""
    titles = {
        "rates":   ("💱 <b>Курс дня</b>", "Курс единый для всех направлений."),
        "tariffs": ("📦 <b>Тарифы</b>",   "Нажмите на строку, чтобы изменить."),
        "photo":   ("🖼 <b>Фото</b>",     "Шаг 2 — каждое второе фото: так в подборку попадает салон."),
    }
    head, hint = titles.get(section, ("⚙️ <b>Настройки</b>", ""))
    keys = [k for k, (_, _, sec) in _EDITABLE.items() if sec == section]

    text = head + "\n\n" + "\n".join(f"• {_fmt_setting(k)}" for k in keys) + f"\n\n<i>{hint}</i>"
    rows = [
        [InlineKeyboardButton(f"✏️ {_EDITABLE[k][0]}", callback_data=f"set:edit:{k}")]
        for k in keys
    ]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="set:back")])
    await _reply(update_or_query, text, InlineKeyboardMarkup(rows))
    return SETTINGS_MENU


async def settings_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    key   = parts[1]

    if key == "close":
        await query.edit_message_text("⚙️ Настройки закрыты.")
        return WAIT_URL

    if key == "back":
        return await _show_settings_menu(query, ctx)

    if key == "admins":
        return await _show_admin_list(query, ctx)

    if key == "brands":
        return await _show_brand_list(query, ctx)

    if key in ("rates", "tariffs", "photo"):
        return await _show_section(query, key)

    if key == "img_debug":
        ctx.user_data["_img_debug_mode"] = True
        await query.edit_message_text(
            "🔍 <b>Отладка фото</b>\n\n"
            "Отправьте ссылку — бот пришлёт все найденные фото пронумерованно, "
            "чтобы было видно, какие попадают в КП.",
            parse_mode="HTML",
        )
        return WAIT_URL

    if key == "edit" and len(parts) > 2:
        skey = parts[2]
        if skey not in _EDITABLE:
            return await _show_settings_menu(query, ctx)
        label, unit, section = _EDITABLE[skey]
        ctx.user_data["_setting_key"] = skey
        await query.edit_message_text(
            f"✏️ <b>{label}</b>\n"
            f"Сейчас: <b>{get_setting(skey)}{(' ' + unit) if unit else ''}</b>\n\n"
            f"Введите новое значение:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data=f"set:{section}")
            ]]),
        )
        return SETTINGS_AWAIT_VALUE

    return await _show_settings_menu(query, ctx)


async def settings_receive_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = ctx.user_data.pop("_setting_key", None)
    if not key or key not in _EDITABLE:
        await update.message.reply_text("❌ Ошибка состояния. Используй /settings заново.")
        return WAIT_URL

    val = _parse_number((update.message.text or "").strip())
    if val is None:
        await update.message.reply_text("Введите число, например: 4900")
        ctx.user_data["_setting_key"] = key
        return SETTINGS_AWAIT_VALUE

    label, unit, section = _EDITABLE[key]

    if key in ("img_count", "img_step", "img_offset"):
        val = max(0, int(val))
        if key in ("img_count", "img_step") and val < 1:
            val = 1

    set_setting(key, str(val))

    # Правка курса руками = курс на сегодня
    if section == "rates":
        user = update.effective_user
        set_setting("rates_date", _today())
        set_setting("rates_set_by", user.first_name or user.username or "")

    await update.message.reply_text(
        f"✅ <b>{label}</b> → <b>{val}{(' ' + unit) if unit else ''}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ К настройкам", callback_data=f"set:{section}")
        ]]),
    )
    return SETTINGS_MENU


# ── Курс дня ──────────────────────────────────────────────────────────────────

def _rates_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💱 Ввести курс", callback_data="rates:set")
    ]])


async def job_ask_rates(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Каждое утро в 8:00 МСК просим админов задать курс на день."""
    _reload_admin_ids()
    r = get_rates()
    text = (
        f"☀️ <b>Доброе утро!</b>\n"
        f"Нужен курс на <b>{_today()}</b>.\n\n"
        f"Вчерашний: EUR→USDT <b>{r['rate_eur_usdt']}</b>, USDT→₽ <b>{r['rate_usdt_rub']}</b>\n\n"
        f"Кто первым введёт — тот курс и встанет на весь день."
    )
    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=admin_id, text=text, parse_mode="HTML", reply_markup=_rates_kb()
            )
        except Exception:
            continue   # админ не начинал диалог с ботом — просто пропускаем


async def cmd_rates(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """/rates — задать курс вручную в любой момент."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Только для администратора.")
        return WAIT_URL
    r = get_rates()
    await update.message.reply_text(
        f"💱 <b>Курс дня</b>\n"
        f"Сейчас: EUR→USDT <b>{r['rate_eur_usdt']}</b>, USDT→₽ <b>{r['rate_usdt_rub']}</b>\n"
        f"Задан: <b>{r['rates_date'] or 'никогда'}</b>"
        + (f" ({r['rates_set_by']})" if r["rates_set_by"] else ""),
        parse_mode="HTML",
        reply_markup=_rates_kb(),
    )
    return WAIT_URL


async def rates_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Ввести курс». Если курс на сегодня уже есть — не пускаем."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in ADMIN_IDS:
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return WAIT_URL

    r = get_rates()
    if r["rates_date"] == _today():
        who = f" ({r['rates_set_by']})" if r["rates_set_by"] else ""
        await query.edit_message_text(
            f"✅ Курс на сегодня уже выставлен{who}:\n"
            f"EUR→USDT <b>{r['rate_eur_usdt']}</b>, USDT→₽ <b>{r['rate_usdt_rub']}</b>\n\n"
            f"<i>Уточните позже — курс меняется раз в день.</i>",
            parse_mode="HTML",
        )
        return WAIT_URL

    await query.edit_message_text(
        f"💱 Курс на <b>{_today()}</b>\n\n"
        f"Введите <b>EUR→USDT</b> (например: 1.1621):",
        parse_mode="HTML",
    )
    return RATES_AWAIT_EUR


async def rates_receive_eur(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    val = _parse_number((update.message.text or "").strip())
    if val is None or val <= 0:
        await update.message.reply_text("Введите число, например: 1.1621")
        return RATES_AWAIT_EUR
    ctx.user_data["_rate_eur_usdt"] = val
    await update.message.reply_text(
        f"✅ EUR→USDT: <b>{val}</b>\n\nТеперь введите <b>USDT→₽</b> (например: 79.7):",
        parse_mode="HTML",
    )
    return RATES_AWAIT_RUB


async def rates_receive_rub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    val = _parse_number((update.message.text or "").strip())
    if val is None or val <= 0:
        await update.message.reply_text("Введите число, например: 79.7")
        return RATES_AWAIT_RUB

    eur = ctx.user_data.pop("_rate_eur_usdt", None)
    if eur is None:
        await update.message.reply_text("❌ Ошибка состояния, начните заново: /rates")
        return WAIT_URL

    # Пока вводили — курс мог задать другой админ
    r = get_rates()
    if r["rates_date"] == _today():
        who = f" ({r['rates_set_by']})" if r["rates_set_by"] else ""
        await update.message.reply_text(
            f"⚠️ Курс на сегодня уже выставлен{who}: "
            f"EUR→USDT <b>{r['rate_eur_usdt']}</b>, USDT→₽ <b>{r['rate_usdt_rub']}</b>\n"
            f"Ваши значения не сохранены.",
            parse_mode="HTML",
        )
        return WAIT_URL

    user = update.effective_user
    who  = user.first_name or user.username or str(user.id)
    set_rates(eur, val, _today(), who)

    await update.message.reply_text(
        f"✅ <b>Курс на {_today()} принят</b>\n"
        f"EUR→USDT: <b>{eur}</b>\n"
        f"USDT→₽: <b>{val}</b>\n\n"
        f"Все расчёты сегодня идут по нему.",
        parse_mode="HTML",
        reply_markup=_main_kb(user.id in ADMIN_IDS),
    )
    return WAIT_URL


# ── Эмодзи марок (премиум custom emoji) ──────────────────────────────────────

async def _show_brand_list(query_or_message, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    brands = get_all_brand_emoji()
    price_id = get_setting("price_emoji_id")

    lines = [
        "😀 <b>Эмодзи марок</b>\n",
        "Бот подставляет эмодзи в КП по марке из названия авто "
        "(«<i>Skoda</i> Superb Combi…» → эмодзи Skoda).\n"
        "Составные марки пишите двумя словами: <code>Alfa Romeo</code>.\n",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if brands:
        for b in brands:
            lines.append(f"• {b['emoji']} {b['brand'].capitalize()}")
            rows.append([InlineKeyboardButton(
                f"❌ {b['brand'].capitalize()}", callback_data=f"brand:del:{b['brand']}"
            )])
    else:
        lines.append("<i>Пока ничего не добавлено.</i>")

    header_id = get_setting("header_emoji_id")
    lines.append(f"\n💸 Эмодзи у цены: <b>{'задан' if price_id else 'обычный 💸'}</b>")
    lines.append(f"🇪🇺 Эмодзи заголовка: <b>{'задан' if header_id else 'обычный 🇪🇺'}</b>")

    rows.append([InlineKeyboardButton("➕ Добавить марку",   callback_data="brand:add")])
    rows.append([InlineKeyboardButton("💸 Эмодзи цены",      callback_data="brand:price")])
    rows.append([InlineKeyboardButton("🇪🇺 Эмодзи заголовка", callback_data="brand:header")])
    rows.append([InlineKeyboardButton("◀️ Назад",            callback_data="brand:back")])

    await _reply(query_or_message, "\n".join(lines), InlineKeyboardMarkup(rows))
    return SETTINGS_MENU


async def brand_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")

    if action[1] == "back":
        return await _show_settings_menu(query, ctx)

    if action[1] == "del":
        remove_brand_emoji(action[2])
        return await _show_brand_list(query, ctx)

    if action[1] == "header":
        ctx.user_data["_brand_target"] = "_header"
        await query.edit_message_text(
            "🇪🇺 Пришлите <b>премиум-эмодзи</b> для первой строки КП — "
            "он встанет после «ДОСТУПЕН В ЕВРОПЕ».\n\n"
            "<i>Просто отправьте его сообщением — бот запомнит.</i>",
            parse_mode="HTML",
        )
        return BRAND_AWAIT_EMOJI

    if action[1] == "price":
        ctx.user_data["_brand_target"] = "_price"
        await query.edit_message_text(
            "💸 Пришлите <b>премиум-эмодзи</b>, который будет стоять перед ценой.\n\n"
            "<i>Просто отправьте его сообщением — бот запомнит.</i>",
            parse_mode="HTML",
        )
        return BRAND_AWAIT_EMOJI

    if action[1] == "add":
        await query.edit_message_text(
            "Введите <b>марку</b> так, как она пишется в названии авто "
            "(например: <code>Skoda</code>):",
            parse_mode="HTML",
        )
        return BRAND_AWAIT_NAME

    return await _show_brand_list(query, ctx)


async def brand_receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # До двух слов — чтобы работали составные марки: Alfa Romeo, Land Rover
    name = " ".join((update.message.text or "").strip().split()[:2])
    if not name:
        await update.message.reply_text("Введите марку, например: Skoda")
        return BRAND_AWAIT_NAME

    ctx.user_data["_brand_target"] = name
    await update.message.reply_text(
        f"Марка: <b>{name}</b>\n\n"
        f"Теперь пришлите <b>премиум-эмодзи</b> этой марки одним сообщением.",
        parse_mode="HTML",
    )
    return BRAND_AWAIT_EMOJI


def _extract_custom_emoji(msg) -> tuple[str, str] | None:
    """(custom_emoji_id, сам символ) из сообщения — текстом или стикером."""
    for ent in list(msg.entities or ()) + list(msg.caption_entities or ()):
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            src = msg.text or msg.caption or ""
            return ent.custom_emoji_id, src[ent.offset:ent.offset + ent.length]
    sticker = getattr(msg, "sticker", None)
    if sticker is not None and getattr(sticker, "custom_emoji_id", None):
        return sticker.custom_emoji_id, sticker.emoji or "🚗"
    return None


async def brand_receive_emoji(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    target = ctx.user_data.pop("_brand_target", "")
    found  = _extract_custom_emoji(update.message)

    if not found:
        ctx.user_data["_brand_target"] = target
        await update.message.reply_text(
            "❌ Это не премиум-эмодзи. Нужен именно анимированный эмодзи из премиум-пака "
            "(обычные смайлики не подойдут). Пришлите ещё раз."
        )
        return BRAND_AWAIT_EMOJI

    emoji_id, char = found
    user = update.effective_user

    if target == "_price":
        set_setting("price_emoji_id", emoji_id)
        set_setting("price_emoji", char)
        await update.message.reply_text(f"✅ Эмодзи цены сохранён: {char}")
    elif target == "_header":
        set_setting("header_emoji_id", emoji_id)
        set_setting("header_emoji", char)
        await update.message.reply_text(f"✅ Эмодзи заголовка сохранён: {char}")
    else:
        set_brand_emoji(target, emoji_id, char, user.first_name or "")
        await update.message.reply_text(
            f"✅ {char} — сохранён для марки <b>{target}</b>.\n"
            f"Теперь во всех КП с этой маркой он подставится автоматически.",
            parse_mode="HTML",
        )
    return await _show_brand_list(update, ctx)


# ── Admin management ──────────────────────────────────────────────────────────

async def _show_admin_list(query_or_message, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Render the admin list with add/remove buttons."""
    admins   = get_all_admins()
    env_ids  = _ENV_ADMIN_IDS

    lines = ["👥 <b>Администраторы</b>\n"]
    rows: list[list[InlineKeyboardButton]] = []

    for a in admins:
        tid  = a["telegram_id"]
        name = a["name"]
        tag  = " 🔒" if tid in env_ids else ""   # env-seed admins can't be removed
        lines.append(f"• {name} (<code>{tid}</code>){tag}")
        if tid not in env_ids:
            rows.append([InlineKeyboardButton(
                f"❌ Удалить {name}", callback_data=f"adm:del:{tid}"
            )])

    # Also show env-admins that aren't in DB yet
    for tid in sorted(env_ids):
        if not any(a["telegram_id"] == tid for a in admins):
            lines.append(f"• <i>из .env</i> (<code>{tid}</code>) 🔒")

    rows.append([InlineKeyboardButton("➕ Добавить админа", callback_data="adm:add")])
    rows.append([InlineKeyboardButton("◀️ Назад",           callback_data="adm:back")])

    text = "\n".join(lines)
    kb   = InlineKeyboardMarkup(rows)

    if hasattr(query_or_message, "edit_message_text"):
        await query_or_message.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await query_or_message.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return ADMIN_LIST


async def admin_mgmt_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle adm: callbacks (list screen)."""
    query = update.callback_query
    await query.answer()
    action = query.data  # e.g. "adm:add", "adm:del:123456", "adm:back"

    if action == "adm:back":
        return await _show_settings_menu(update, ctx)

    if action == "adm:add":
        await query.edit_message_text(
            "👤 Введите <b>имя</b> нового администратора:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="adm:back")
            ]]),
        )
        return ADMIN_ADD_NAME

    if action.startswith("adm:del:"):
        tid = int(action.split(":")[2])
        remove_admin(tid)
        _reload_admin_ids()
        await query.answer("✅ Админ удалён", show_alert=True)
        return await _show_admin_list(query, ctx)

    return ADMIN_LIST


async def admin_receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """State ADMIN_ADD_NAME — got the name, now ask for Telegram ID."""
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Имя не может быть пустым. Введите имя:")
        return ADMIN_ADD_NAME

    ctx.user_data["_new_admin_name"] = name
    await update.message.reply_text(
        f"🆔 Теперь введите <b>Telegram ID</b> для <b>{name}</b>:\n"
        f"<i>(только цифры, например: 123456789)</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="adm:back_to_list")
        ]]),
    )
    return ADMIN_ADD_ID


async def admin_receive_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """State ADMIN_ADD_ID — got the Telegram ID, save and return to list."""
    raw  = (update.message.text or "").strip()
    name = ctx.user_data.pop("_new_admin_name", "")

    if not raw.isdigit():
        await update.message.reply_text(
            "❌ ID должен содержать только цифры. Попробуйте снова:"
        )
        ctx.user_data["_new_admin_name"] = name
        return ADMIN_ADD_ID

    tid = int(raw)
    add_admin(tid, name)
    _reload_admin_ids()

    msg = await update.message.reply_text(
        f"✅ <b>{name}</b> (<code>{tid}</code>) добавлен как администратор.",
        parse_mode="HTML",
    )
    # Show updated admin list as a new message
    return await _show_admin_list(msg, ctx)


async def admin_cancel_to_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle ❌ Отмена inside add-admin flow — return to admin list."""
    query = update.callback_query
    await query.answer()
    ctx.user_data.pop("_new_admin_name", None)
    return await _show_admin_list(query, ctx)


# ── /cancel ───────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text(
        "↩️ Отменено. Отправь ссылку для нового расчёта.",
        reply_markup=_main_kb(update.effective_user.id in ADMIN_IDS),
    )
    return WAIT_URL


# ── Main ──────────────────────────────────────────────────────────────────────

class StaleCallbackHandler(CallbackQueryHandler):
    """
    Ловит нажатия кнопок, которые диалог обработать не может — например, из
    сообщений, отправленных до перезапуска бота. Без него такое нажатие
    остаётся без ответа и выглядит как зависший бот.

    Работает в отдельной группе, поэтому сначала спрашивает у диалога,
    справится ли он сам: PTB отдаёт апдейт во все группы подряд, и без этой
    проверки перехватчик отвечал бы поверх нормальных кнопок.
    """

    def __init__(self, conv: ConversationHandler, callback):
        super().__init__(callback)
        self._conv = conv

    def check_update(self, update: object):
        if self._conv.check_update(update):
            return None
        return super().check_update(update)


async def stale_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на устаревшую кнопку — вместо молчания."""
    query = update.callback_query
    await query.answer(
        "Кнопка устарела — бот перезапускался.\nОткройте раздел заново кнопкой снизу.",
        show_alert=True,
    )


async def _post_init(app) -> None:
    """Кнопка «Меню» у бота открывает мини-апп."""
    if not WEB_APP_URL:
        return
    try:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Приложение", web_app=WebAppInfo(url=f"{WEB_APP_URL}/")
            )
        )
        print(f"Mini App menu button → {WEB_APP_URL}")
    except Exception as e:
        print(f"WARNING: не удалось поставить кнопку мини-аппа: {e}")


def build_application(token: str):
    """Собирает Application со всеми хендлерами (вынесено для тестов)."""
    app = ApplicationBuilder().token(token).post_init(_post_init).build()

    # Exclude keyboard button texts from free-text state handlers
    _text = filters.TEXT & ~filters.COMMAND & ~_KB_FILTER

    conv = ConversationHandler(
        # ВАЖНО: всё, что пользователь может нажать «на холодную», обязано быть
        # точкой входа. Состояния диалога живут в памяти и теряются при
        # перезапуске бота (то есть при каждом деплое). Если кнопка описана
        # только в fallbacks, после рестарта она молчит до /start.
        entry_points=[
            CommandHandler("start",    cmd_start),
            CommandHandler("pending",  cmd_pending),
            CommandHandler("settings", cmd_settings),
            CommandHandler("rates",    cmd_rates),
            CommandHandler("app",      cmd_app),
            # Кнопки нижней клавиатуры
            MessageHandler(filters.Regex(rf"^{re.escape(_BTN_HISTORY)}$"),  show_history),
            MessageHandler(filters.Regex(rf"^{re.escape(_BTN_PENDING)}$"),  cmd_pending),
            MessageHandler(filters.Regex(rf"^{re.escape(_BTN_SETTINGS)}$"), cmd_settings),
            # Кнопки под сообщениями, которые могли прийти до перезапуска:
            # утренний запрос курса, меню настроек, история, выбор фото —
            # все они читают данные из базы, поэтому работают и без диалога
            CallbackQueryHandler(rates_start,        pattern=r"^rates:"),
            CallbackQueryHandler(settings_button,    pattern=r"^set:"),
            CallbackQueryHandler(brand_button,       pattern=r"^brand:"),
            CallbackQueryHandler(admin_mgmt_button,  pattern=r"^adm:"),
            CallbackQueryHandler(history_open_item,  pattern=r"^hist:\d+$"),
            CallbackQueryHandler(pending_pick,       pattern=r"^pending_pick:"),
            CallbackQueryHandler(photo_choice_auto,  pattern=r"^autokp:"),
            CallbackQueryHandler(photo_choice_skip,  pattern=r"^nokp:"),
            CallbackQueryHandler(kp_edit_button,     pattern=r"^kpedit:"),
            MessageHandler(_link_filter, receive_url),
        ],
        states={
            WAIT_URL: [
                MessageHandler(_text, receive_url),
            ],
            RATES_AWAIT_EUR: [
                MessageHandler(_text, rates_receive_eur),
            ],
            RATES_AWAIT_RUB: [
                MessageHandler(_text, rates_receive_rub),
            ],
            ASK_DIRECTION: [
                CallbackQueryHandler(receive_direction, pattern=r"^dir:"),
            ],
            ASK_COUNTERPARTY: [
                MessageHandler(_text, receive_counterparty),
            ],
            ASK_VAT: [
                CallbackQueryHandler(receive_vat, pattern=r"^vat:"),
            ],
            ASK_VAT_MANUAL: [
                MessageHandler(_text, receive_vat_manual),
            ],
            ASK_BUYBACK: [
                CallbackQueryHandler(receive_buyback, pattern=r"^buyback:"),
            ],
            ASK_BUYBACK_MANUAL: [
                MessageHandler(_text, receive_buyback_manual),
            ],
            ASK_CUSTOMS: [
                CallbackQueryHandler(receive_customs_defer, pattern=r"^customs:defer$"),
                MessageHandler(_text, receive_customs_value),
            ],
            ASK_UTIL: [
                MessageHandler(_text, receive_util),
            ],
            ASK_EVACUATOR: [
                MessageHandler(_text, receive_evacuator),
            ],
            ASK_CUSTOMS_TKS: [
                MessageHandler(_text, receive_customs_tks),
            ],
            CONFIRM: [
                CallbackQueryHandler(receive_confirm, pattern=r"^confirm:"),
            ],
            SETTINGS_MENU: [
                CallbackQueryHandler(settings_button, pattern=r"^set:"),
                CallbackQueryHandler(brand_button,    pattern=r"^brand:"),
                CallbackQueryHandler(admin_mgmt_button, pattern=r"^adm:"),
            ],
            BRAND_AWAIT_NAME: [
                CallbackQueryHandler(brand_button, pattern=r"^brand:"),
                MessageHandler(_text, brand_receive_name),
            ],
            BRAND_AWAIT_EMOJI: [
                CallbackQueryHandler(brand_button, pattern=r"^brand:"),
                MessageHandler(_text | filters.Sticker.ALL, brand_receive_emoji),
            ],
            SETTINGS_AWAIT_VALUE: [
                MessageHandler(_text, settings_receive_value),
            ],
            PENDING_CHOOSE: [
                CallbackQueryHandler(pending_pick, pattern=r"^pending_pick:"),
            ],
            PENDING_CUSTOMS: [
                MessageHandler(_text, pending_customs),
            ],
            PENDING_UTIL: [
                MessageHandler(_text, pending_util),
            ],
            HISTORY_LIST: [
                CallbackQueryHandler(history_open_item, pattern=r"^hist:\d+$"),
            ],
            HISTORY_ITEM: [
                CallbackQueryHandler(history_edit_pick, pattern=r"^hedit:"),
                CallbackQueryHandler(show_history,      pattern=r"^hist:back$"),
            ],
            HISTORY_EDIT_VALUE: [
                CallbackQueryHandler(history_edit_pick, pattern=r"^hedit:"),
                MessageHandler(_text, history_edit_value),
            ],
            KP_PHOTO_EDIT: [
                CallbackQueryHandler(kp_edit_button, pattern=r"^kpedit:"),
            ],
            PHOTO_CHOICE: [
                CallbackQueryHandler(photo_choice_auto, pattern=r"^autokp:"),
                CallbackQueryHandler(photo_choice_skip, pattern=r"^nokp:"),
            ],
            ADMIN_LIST: [
                CallbackQueryHandler(admin_mgmt_button,   pattern=r"^adm:"),
            ],
            ADMIN_ADD_NAME: [
                CallbackQueryHandler(admin_cancel_to_list, pattern=r"^adm:back"),
                MessageHandler(_text, admin_receive_name),
            ],
            ADMIN_ADD_ID: [
                CallbackQueryHandler(admin_cancel_to_list, pattern=r"^adm:back"),
                MessageHandler(_text, admin_receive_id),
            ],
        },
        fallbacks=[
            CommandHandler("cancel",   cmd_cancel),
            CommandHandler("start",    cmd_start),
            CommandHandler("pending",  cmd_pending),
            CommandHandler("settings", cmd_settings),
            CommandHandler("rates",    cmd_rates),
            CommandHandler("app",      cmd_app),
            CallbackQueryHandler(rates_start, pattern=r"^rates:"),
            # Persistent keyboard buttons work from ANY state
            MessageHandler(filters.Regex(rf"^{re.escape(_BTN_HISTORY)}$"),  show_history),
            MessageHandler(filters.Regex(rf"^{re.escape(_BTN_PENDING)}$"),  cmd_pending),
            MessageHandler(filters.Regex(rf"^{re.escape(_BTN_SETTINGS)}$"), cmd_settings),
            # Settings callbacks reachable from any state
            CallbackQueryHandler(settings_button, pattern=r"^set:"),
        ],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(conv)
    # Отдельная группа: отвечает на кнопки, до которых диалог не дотянулся
    app.add_handler(StaleCallbackHandler(conv, stale_button), group=1)

    # Утренний запрос курса — 8:00 по Москве
    if app.job_queue:
        app.job_queue.run_daily(job_ask_rates, time=RATES_ASK_TIME, name="ask_rates")
        print(f"Daily rates prompt scheduled at {RATES_ASK_TIME}")
    else:
        print("WARNING: JobQueue недоступен — утренний запрос курса не будет работать. "
              "Установите python-telegram-bot[job-queue].")

    return app


def main() -> None:
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    init_db()
    _reload_admin_ids()
    print(f"Database initialized. Admin IDs: {ADMIN_IDS}")

    app = build_application(TOKEN)

    print("Bot started. Press Ctrl+C to stop.")
    # Python 3.14: get_event_loop() больше не создаёт loop автоматически,
    # а PTB 21.6 на него рассчитывает — создаём явно.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
