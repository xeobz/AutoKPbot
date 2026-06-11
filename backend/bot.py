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
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    Update,
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

from ai import generate_kp_text
from scraper import scrape_mobile_de
from storage import (
    complete_pending,
    get_all_settings,
    get_admin_ids,
    add_admin,
    remove_admin,
    get_all_admins,
    get_history_by_id,
    get_history_for_user,
    get_pending_by_id,
    get_pending_for_user,
    get_setting,
    init_db,
    save_history,
    save_pending,
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
) = range(22)

MOBILE_DE_RE = re.compile(r"https?://(?:www\.)?mobile\.de\S+", re.IGNORECASE)

VAT_OPTIONS = [
    ("🇩🇪 19% (Германия)", "1.19"),
    ("🇧🇾 17% (Беларусь)", "1.17"),
    ("🇧🇪 21% (Бельгия)",  "1.21"),
]
BUYBACK_PCTS = list(range(5, 16))   # 5–15 %

# Keyboard button labels (used for routing)
_BTN_HISTORY  = "📋 История"
_BTN_PENDING  = "⏸ Незавершённые"
_BTN_SETTINGS = "⚙️ Настройки"

# Filter: all keyboard button texts (to exclude from text input handlers)
_KB_FILTER = filters.Regex(
    rf"^({re.escape(_BTN_HISTORY)}|{re.escape(_BTN_PENDING)}|{re.escape(_BTN_SETTINGS)})$"
)
_mobile_de_filter = filters.TEXT & ~filters.COMMAND & filters.Regex(r"mobile\.de")


# ── Keyboard helpers ──────────────────────────────────────────────────────────

def _main_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    row = [_BTN_HISTORY, _BTN_PENDING]
    if is_admin:
        row.append(_BTN_SETTINGS)
    return ReplyKeyboardMarkup(
        [row],
        resize_keyboard=True,
        input_field_placeholder="Вставьте ссылку mobile.de…",
    )


def fmt_eur(v: float) -> str:
    return f"{v:,.0f} €".replace(",", " ")


def fmt_rub(v: float) -> str:
    return f"{v:,.0f} ₽".replace(",", " ")


# ── Number parser (handles non-breaking spaces from mobile keyboards) ─────────

def _parse_number(text: str) -> float | None:
    cleaned = re.sub(r"[\s\xa0  ]+", "", text).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ── Data helpers ──────────────────────────────────────────────────────────────

def _extract_url(text: str) -> str | None:
    m = MOBILE_DE_RE.search(text)
    return m.group(0) if m else None


def _get_rates() -> dict:
    """Rates for ЕС/Минск (sheet 1)."""
    s = get_all_settings()
    return {
        "rate_n":  float(s.get("rate_n",  1.1621)),
        "rate_p":  float(s.get("rate_p",  79.7)),
        "r_value": float(s.get("r_value", 45000)),
    }


def _get_rates_v2() -> dict:
    """Rates for ЕС/Культ40 and ЕС-МСК (sheets 2 & 3)."""
    s = get_all_settings()
    return {
        "rate_eur_usd": float(s.get("rate_eur_usd", 1.10)),
        "rate_usd_rub": float(s.get("rate_usd_rub", 80.0)),
    }


def _calc(d: dict) -> dict:
    """Compute all derived values for ЕС/Минск (sheet 1)."""
    g   = d.get("price_eur", 0) or 0
    vat = d.get("vat", 1.19)
    h   = g / vat
    k   = d.get("buyback_val", 0) or 0
    j   = 5300
    l_  = h * 0.013 + 100
    m   = h + j + k + l_ + 350
    n   = d.get("rate_n", 1.1621)
    p   = d.get("rate_p", 79.7)
    o   = m * n
    q   = o * p
    r   = d.get("r_value", 45000) or 45000
    s   = d.get("customs_eur", 0) or 0
    t   = s * n * p
    u   = d.get("util_rub", 0) or 0
    v   = q + r + t + u
    return dict(g=g, vat=vat, h=h, k=k, j=j, l=l_, m=m, n=n, p=p, o=o, q=q, r=r, s=s, t=t, u=u, v=v)


def _calc_v2(d: dict) -> dict:
    """Compute all derived values for ЕС/Культ40 and ЕС-МСК (sheets 2 & 3)."""
    direction = d.get("direction", "kult40")
    f   = d.get("price_eur", 0) or 0          # F — БРУТТО
    vat = d.get("vat", 1.19)
    g   = f / vat                              # G — НЕТТО
    j   = float(d.get("logistics",            # I — Логистика
                       4100 if direction == "kult40" else 2500))
    k   = d.get("buyback_val", g * 0.08)      # J — Выкуп
    l_  = g * 0.013 + 100                     # K — Инвойс
    l_t = g + j + k + l_ + 350               # L — Итого EUR
    m   = d.get("rate_eur_usd", 1.10)         # M — КК EUR/USD
    n   = l_t * m                             # N — USDT
    o   = d.get("rate_usd_rub", 80.0)         # O — USD/RUB
    p   = n * o                               # P — RUB
    q   = 100_000.0                           # Q — Брокер (fixed)
    r   = d.get("evacuator_rub", 0) or 0      # R — Эвакуатор (Культ40)
    s   = d.get("customs_tks_rub", 0) or 0    # S (Культ40) / R (МСК) — Таможня ТКС
    t   = 5_200.0                             # T (Культ40) / S (МСК) — Утиль (fixed)
    v   = p + q + r + s + t if direction == "kult40" else p + q + s + t
    return dict(f=f, vat=vat, g=g, j=j, k=k, l=l_, l_t=l_t,
                m=m, n=n, o=o, p=p, q=q, r=r, s=s, t=t, v=v)


def _build_card(d: dict, title: str = "📋 <b>Итоговая карточка</b>") -> str:
    """Dispatch to the correct card builder based on direction."""
    if d.get("direction") in ("kult40", "msk"):
        return _build_card_v2(d, title)
    return _build_card_minsk(d, title)


def _build_card_minsk(d: dict, title: str = "📋 <b>Итоговая карточка</b>") -> str:
    c   = _calc(d)
    pct = d.get("buyback_pct", "—")
    lines = [
        title, "",
        f"🚗 <b>{d.get('car_name', '—')}</b>",
        f"📍 Направление: 🏙 ЕС/Минск",
        f"👤 Контрагент: {d.get('counterparty', '—')}",
        f"🔗 <a href=\"{d.get('url','')}\">Объявление</a>",
        "",
        f"💶 БРУТТО (G): <b>{fmt_eur(c['g'])}</b>",
        f"📊 НДС: {c['vat']}  →  НЕТТО: <b>{fmt_eur(c['h'])}</b>",
        f"🏦 Выкуп {pct}% (K): {fmt_eur(c['k'])}",
        f"📦 Логистика (J): {fmt_eur(c['j'])}",
        f"📄 Инвойс (L): {fmt_eur(c['l'])}",
        f"💰 Итого EUR (M): <b>{fmt_eur(c['m'])}</b>",
        "",
        f"📈 EUR→USDT (N): {c['n']}",
        f"💵 USDT (O): {fmt_eur(c['o'])}",
        f"📈 USDT→₽ (P): {c['p']}",
        f"💵 Итого ₽ (Q): <b>{fmt_rub(c['q'])}</b>",
        "",
        f"🛃 Таможня EUR (S): {fmt_eur(c['s']) if c['s'] else '⏳ не указана'}",
        f"🛃 Таможня ₽ (T): {fmt_rub(c['t']) if c['s'] else '⏳'}",
        f"♻️ Утиль (U): {fmt_rub(c['u']) if c['u'] else '⏳ не указан'}",
        "",
        f"✅ <b>Итого с утильсбором (V): {fmt_rub(c['v'])}</b>",
    ]
    return "\n".join(lines)


def _build_card_v2(d: dict, title: str = "📋 <b>Итоговая карточка</b>") -> str:
    c         = _calc_v2(d)
    direction = d.get("direction", "kult40")
    pct       = d.get("buyback_pct", "—")
    dir_label = "🏭 ЕС/Культ40" if direction == "kult40" else "🌆 ЕС-МСК"
    lines = [
        title, "",
        f"🚗 <b>{d.get('car_name', '—')}</b>",
        f"📍 Направление: {dir_label}",
        f"👤 Контрагент: {d.get('counterparty', '—')}",
        f"🔗 <a href=\"{d.get('url','')}\">Объявление</a>",
        "",
        f"💶 БРУТТО (F): <b>{fmt_eur(c['f'])}</b>",
        f"📊 НДС: {c['vat']}  →  НЕТТО: <b>{fmt_eur(c['g'])}</b>",
        f"🏦 Выкуп {pct}% (J): {fmt_eur(c['k'])}",
        f"📦 Логистика (I): {fmt_eur(c['j'])}",
        f"📄 Инвойс (K): {fmt_eur(c['l'])}",
        f"💰 Итого EUR (L): <b>{fmt_eur(c['l_t'])}</b>",
        "",
        f"📈 КК EUR/USD (M): {c['m']}",
        f"💵 USDT (N): {fmt_eur(c['n'])}",
        f"📈 USD→₽ ЦБ (O): {c['o']}",
        f"💵 Итого ₽ (P): <b>{fmt_rub(c['p'])}</b>",
        "",
        f"🏢 Брокер ПТО+ЭПС (Q): {fmt_rub(c['q'])}",
    ]
    if direction == "kult40":
        lines += [
            f"🚛 Эвакуатор СПБ-МСК (R): {fmt_rub(c['r']) if c['r'] else '⏳ не указан'}",
            f"🛃 Таможня ТКС (S): {fmt_rub(c['s']) if c['s'] else '⏳ не указана'}",
            f"♻️ Утиль (T): {fmt_rub(c['t'])}",
            "",
            f"✅ <b>Итого (U): {fmt_rub(c['v'])}</b>",
        ]
    else:
        lines += [
            f"🛃 Таможня ТКС (R): {fmt_rub(c['s']) if c['s'] else '⏳ не указана'}",
            f"♻️ Утиль (S): {fmt_rub(c['t'])}",
            "",
            f"✅ <b>Итого (T): {fmt_rub(c['v'])}</b>",
        ]
    return "\n".join(lines)


def _build_buyback_keyboard(h: float) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for pct in BUYBACK_PCTS:
        k = h * pct / 100
        row.append(InlineKeyboardButton(f"{pct}% — {fmt_eur(k)}", callback_data=f"buyback:{pct}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
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
    """
    Generate and send КП (AI text + photos).
    Returns a dict with photo message IDs for later edit_message_media swaps,
    or None if no photos were sent.
    """
    from storage import get_setting

    c = _calc(d)
    try:
        kp_text = await generate_kp_text(
            make=d.get("make", ""),
            model=d.get("model", ""),
            year=d.get("year", ""),
            mileage=d.get("mileage"),
            color=d.get("color", ""),
            features=d.get("features", []),
            price_eur=c["g"],
            customs_eur=c["g"] + c["s"],
            price_rub_turnkey=c["q"] + c["t"],
            price_rub_util=c["u"],
            price_rub_epts=c["r"],
            contact=CONTACT,
            lot_number=str(car_num),
        )
    except Exception:
        kp_text = None

    # Cluster algorithm already returns only gallery photos — just take first N
    all_photos = d.get("photos", [])
    try:
        count = int(get_setting("img_count"))
    except (ValueError, TypeError):
        count = 6
    photos = all_photos[:count]

    photo_msg_ids: list[int] = []

    # Telegram caption limit is 1024 chars — KP text goes on the first photo
    CAPTION_LIMIT = 1024
    caption: str | None = None
    if kp_text:
        if len(kp_text) <= CAPTION_LIMIT:
            caption = kp_text
        else:
            # Truncate at last newline before limit so HTML tags don't break mid-tag
            cut = kp_text.rfind("\n", 0, CAPTION_LIMIT - 1)
            caption = kp_text[: cut if cut > 0 else CAPTION_LIMIT - 1] + "…"

    if not photos:
        if kp_text:
            await bot.send_message(chat_id=chat_id, text=kp_text, parse_mode="HTML")
        return None

    if len(photos) == 1:
        msg = await bot.send_photo(
            chat_id=chat_id,
            photo=photos[0],
            caption=caption,
            parse_mode="HTML" if caption else None,
        )
        photo_msg_ids = [msg.message_id]
    else:
        media: list[InputMediaPhoto] = [InputMediaPhoto(media=url) for url in photos]
        if caption:
            media[0] = InputMediaPhoto(media=photos[0], caption=caption, parse_mode="HTML")
        try:
            msgs = await bot.send_media_group(chat_id=chat_id, media=media)
            photo_msg_ids = [m.message_id for m in msgs]
        except Exception:
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=photos[0],
                caption=caption,
                parse_mode="HTML" if caption else None,
            )
            photo_msg_ids = [msg.message_id]

    return {
        "chat_id":       chat_id,
        "photo_msg_ids": photo_msg_ids,
        "used_photos":   list(photos),    # currently shown (mutable, updated on swap)
        "shown_photos":  set(photos),     # ALL ever shown — excluded from pool forever
        "all_photos":    all_photos,      # full gallery
        "caption":       caption,         # preserved when photo #0 is swapped out
        "caption_idx":   0,               # index of the photo that carries the caption
    }


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user     = update.effective_user
    is_admin = user.id in ADMIN_IDS
    await update.message.reply_text(
        "👋 Привет! Отправь ссылку с <b>mobile.de</b> — начнём расчёт.\n\n"
        "Команды:\n"
        "/pending — незавершённые запросы\n"
        "/settings — настройки курсов (для администратора)",
        parse_mode="HTML",
        reply_markup=_main_kb(is_admin),
    )
    return WAIT_URL


# ── Step 1: receive URL ───────────────────────────────────────────────────────

async def receive_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    url = _extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text(
            "❌ Не нашёл ссылку mobile.de. Отправь полную ссылку.",
            reply_markup=_main_kb(update.effective_user.id in ADMIN_IDS),
        )
        return WAIT_URL

    # ── Photo debug mode (🖼 Отладка фото in settings) ───────────────────────
    if ctx.user_data.pop("_img_debug_mode", False):
        wait_msg = await update.message.reply_text("⏳ Загружаю объявление…")
        try:
            result = await scrape_mobile_de(url)
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

    # Load rates + logistics for the chosen sheet
    if direction == "minsk":
        ctx.user_data.update(_get_rates())
    else:
        ctx.user_data.update(_get_rates_v2())
        s = get_all_settings()
        ctx.user_data["logistics"] = float(
            s.get("logistics_kult40" if direction == "kult40" else "logistics_msk",
                  4100 if direction == "kult40" else 2500)
        )

    url = ctx.user_data.get("url", "")
    ctx.user_data["_scraping"] = True
    await query.edit_message_text("⏳ Загружаю объявление…")

    try:
        scraped = await scrape_mobile_de(url)
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
    car_name = f"{make} {model} {year}".strip()

    ctx.user_data.update({
        "car_name":  car_name,
        "price_eur": price,
        "make":      make,
        "model":     model,
        "year":      year,
        "mileage":   scraped.get("mileage"),
        "color":     scraped.get("color", ""),
        "features":  scraped.get("features", []),
        "photos":    scraped.get("photos", []),
        "all_photos": scraped.get("all_photos", []),
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

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"vat:{val}")]
        for label, val in VAT_OPTIONS
    ])
    await update.message.reply_text(
        f"👤 Контрагент: <b>{counterparty}({manager})</b>\n\n"
        "Выберите <b>НДС</b> страны продавца:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return ASK_VAT


# ── Step 3: VAT ───────────────────────────────────────────────────────────────

async def receive_vat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    vat = float(query.data.split(":")[1])
    ctx.user_data["vat"] = vat

    h  = ctx.user_data["price_eur"] / vat
    kb = _build_buyback_keyboard(h)
    await query.edit_message_text(
        f"✅ НДС: <b>{vat}</b> → НЕТТО: <b>{fmt_eur(h)}</b>\n\n"
        "Выберите <b>процент выкупа</b>:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return ASK_BUYBACK


# ── Step 4: buyback % ─────────────────────────────────────────────────────────

async def receive_buyback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    pct = int(query.data.split(":")[1])

    g   = ctx.user_data["price_eur"]
    vat = ctx.user_data["vat"]
    h   = g / vat
    k   = h * pct / 100

    ctx.user_data["buyback_pct"] = pct
    ctx.user_data["buyback_val"] = round(k, 2)

    direction = ctx.user_data.get("direction", "minsk")

    if direction == "kult40":
        await query.edit_message_text(
            f"✅ Выкуп <b>{pct}%</b> → <b>{fmt_eur(k)}</b>\n\n"
            f"Введите <b>стоимость эвакуатора СПБ-МСК</b> (руб.):",
            parse_mode="HTML",
        )
        return ASK_EVACUATOR

    if direction == "msk":
        await query.edit_message_text(
            f"✅ Выкуп <b>{pct}%</b> → <b>{fmt_eur(k)}</b>\n\n"
            f"Введите <b>таможню ТКС</b> (руб.):",
            parse_mode="HTML",
        )
        return ASK_CUSTOMS_TKS

    # minsk — original flow
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏸ Отложить — введу позже", callback_data="customs:defer")
    ]])
    await query.edit_message_text(
        f"✅ Выкуп <b>{pct}%</b> → K = <b>{fmt_eur(k)}</b>\n\n"
        f"Введите <b>таможню РБ</b> в EUR\n(или нажмите «Отложить»):",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return ASK_CUSTOMS


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
        f"👤 {d.get('counterparty')}\n\n"
        f"⏳ Генерирую КП…",
        parse_mode="HTML",
    )

    chat_id       = query.message.chat_id
    data_snapshot = dict(d)
    ctx.user_data.clear()

    kp_result = await _send_kp(chat_id, car_num, data_snapshot, ctx.bot)
    if kp_result:
        ctx.user_data["_kp_edit"] = kp_result
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
        "buyback_pct":     "процент выкупа от 1 до 30 (например: 10)",
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
        if val is None or not (1 <= val <= 30):
            await update.message.reply_text("Введите процент от 1 до 30:")
            return HISTORY_EDIT_VALUE
        pct = int(val)
        g   = data.get("price_eur", 0)
        vat = data.get("vat", 1.19)
        h   = g / vat
        data["buyback_pct"] = pct
        data["buyback_val"] = round(h * pct / 100, 2)

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
        f"✅ <b>Записано #{car_num}</b>\n🚗 {d.get('car_name')}\n\n⏳ Генерирую КП…",
        parse_mode="HTML",
    )

    chat_id       = update.effective_chat.id
    data_snapshot = dict(d)
    ctx.user_data.clear()

    kp_result = await _send_kp(chat_id, car_num, data_snapshot, ctx.bot)
    if kp_result:
        ctx.user_data["_kp_edit"] = kp_result
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

    if action == "done" or not kp:
        ctx.user_data.pop("_kp_edit", None)
        await query.edit_message_text("✅ КП готов.")
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


async def _show_settings_menu(update_or_query, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    s    = get_all_settings()
    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"<b>ЕС/Минск</b>\n"
        f"  N — EUR/USDT: <b>{s.get('rate_n', '?')}</b>\n"
        f"  P — USDT/₽:   <b>{s.get('rate_p', '?')}</b>\n"
        f"  R — ЭПТС/СБКТС: <b>{s.get('r_value', '?')} ₽</b>\n\n"
        f"<b>ЕС/Культ40 + ЕС-МСК</b>\n"
        f"  M — EUR/USD КК: <b>{s.get('rate_eur_usd', '?')}</b>\n"
        f"  O — USD/₽ ЦБ:   <b>{s.get('rate_usd_rub', '?')}</b>\n\n"
        f"Нажмите кнопку для изменения:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Курс N (EUR/USDT)",   callback_data="set:rate_n")],
        [InlineKeyboardButton("📈 Курс P (USDT/₽)",     callback_data="set:rate_p")],
        [InlineKeyboardButton("🔧 ЭПТС/СБКТС (₽)",     callback_data="set:r_value")],
        [InlineKeyboardButton("💱 КК M (EUR/USD)",      callback_data="set:rate_eur_usd")],
        [InlineKeyboardButton("💵 Курс O (USD/₽ ЦБ)",  callback_data="set:rate_usd_rub")],
        [InlineKeyboardButton("👥 Администраторы",      callback_data="set:admins")],
        [InlineKeyboardButton("🖼 Отладка фото",        callback_data="set:img_debug")],
        [InlineKeyboardButton("❌ Закрыть",           callback_data="set:close")],
    ])
    if hasattr(update_or_query, "callback_query") and update_or_query.callback_query:
        await update_or_query.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    elif hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return SETTINGS_MENU


async def settings_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key   = query.data.split(":")[1]

    if key == "close":
        await query.edit_message_text("⚙️ Настройки закрыты.")
        return WAIT_URL

    if key == "admins":
        return await _show_admin_list(query, ctx)

    if key == "img_debug":
        s = get_all_settings()
        ctx.user_data["_img_debug_mode"] = True
        await query.edit_message_text(
            f"🖼 <b>Отладка фото</b>\n\n"
            f"Текущие настройки:\n"
            f"• Пропустить первых: <b>{s.get('img_offset', 0)}</b> фото\n"
            f"• Взять: <b>{s.get('img_count', 6)}</b> фото\n\n"
            f"Отправьте ссылку <b>mobile.de</b> — бот пришлёт все найденные фото пронумерованно.",
            parse_mode="HTML",
        )
        return WAIT_URL

    labels = {
        "rate_n":       "курс N (EUR/USDT)",
        "rate_p":       "курс P (USDT/₽)",
        "r_value":      "ЭПТС/СБКТС ₽",
        "rate_eur_usd": "КК M (EUR/USD, 4 знака после запятой)",
        "rate_usd_rub": "курс O (USD/₽ от ЦБ)",
    }
    ctx.user_data["_setting_key"] = key
    await query.edit_message_text(
        f"Введите новое значение для <b>{labels.get(key, key)}</b>:",
        parse_mode="HTML",
    )
    return SETTINGS_AWAIT_VALUE


async def settings_receive_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = ctx.user_data.pop("_setting_key", None)
    if not key:
        await update.message.reply_text("❌ Ошибка состояния. Используй /settings заново.")
        return WAIT_URL

    val = _parse_number((update.message.text or "").strip())
    if val is None:
        await update.message.reply_text("Введите число, например: 1.1621")
        ctx.user_data["_setting_key"] = key
        return SETTINGS_AWAIT_VALUE

    labels = {
        "rate_n":       "Курс N (EUR/USDT)",
        "rate_p":       "Курс P (USDT/₽)",
        "r_value":      "ЭПТС/СБКТС ₽",
        "rate_eur_usd": "КК M (EUR/USD)",
        "rate_usd_rub": "Курс O (USD/₽ ЦБ)",
    }
    set_setting(key, str(val))
    await update.message.reply_text(
        f"✅ <b>{labels.get(key, key)}</b> → <b>{val}</b>\n\n"
        f"Используй /settings для изменения других значений.\n"
        f"Отправь ссылку mobile.de для нового расчёта.",
        parse_mode="HTML",
    )
    return WAIT_URL


# ── Photo debug (settings) ────────────────────────────────────────────────────

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

def main() -> None:
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    init_db()
    _reload_admin_ids()
    print(f"Database initialized. Admin IDs: {ADMIN_IDS}")

    app = ApplicationBuilder().token(TOKEN).build()

    # Exclude keyboard button texts from free-text state handlers
    _text = filters.TEXT & ~filters.COMMAND & ~_KB_FILTER

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start",    cmd_start),
            CommandHandler("pending",  cmd_pending),
            CommandHandler("settings", cmd_settings),
            MessageHandler(_mobile_de_filter, receive_url),
        ],
        states={
            WAIT_URL: [
                MessageHandler(_text, receive_url),
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
            ASK_BUYBACK: [
                CallbackQueryHandler(receive_buyback, pattern=r"^buyback:"),
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

    print("Bot started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
