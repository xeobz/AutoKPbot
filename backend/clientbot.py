"""
Клиентский бот: контрагент пересылает КП Montaro и получает брендированную
презентацию — PDF, картинку для сторис или оба файла.

КП → формат → логотип → наценка → конструктор фото (или сразу) → файлы в чат.
Принимаются только подлинные КП: номер лота в подписи — ссылка на снимок КП.
"""
import io
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, Message, Update,
                      WebAppInfo)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

load_dotenv(Path(__file__).parent.parent / ".env")

import presentation  # noqa: E402
from clientsend import deliver_job  # noqa: E402
from storage import (create_job, find_snapshot, get_client_profile, get_snapshot,  # noqa: E402
                     init_db, save_client_logo, save_client_markup)

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("autokp.clientbot")

TOKEN = os.getenv("CLIENT_BOT_TOKEN", "")
WEB_APP_URL = os.getenv("WEB_APP_URL", "").rstrip("/")
LOGO_DIR = presentation.MEDIA_DIR / "logos"

FORMAT_NAMES = {"pdf": "📄 PDF", "story": "📱 Сторис", "both": "📄 + 📱 Оба"}
_TOKEN_RE = re.compile(r"/k/([A-Za-z0-9_\-]{8,})")


def _fmt_rub(v: int) -> str:
    return f"{int(v):,}".replace(",", " ") + " ₽"


def parse_markup(text: str) -> int | None:
    """
    Наценка в рублях. Люди пишут по-разному: «500 000», «500к», «500 тыс»,
    «0,5 млн». Голые цифры из «500к» дали бы 500 ₽ — отсюда разбор суффиксов.
    """
    t = (text or "").lower().replace("\u00a0", " ").replace("₽", "").replace("руб", "").strip()
    m = re.fullmatch(r"([\d\s]+(?:[.,]\d+)?)\s*(к|k|тыс\.?|тысяч[аи]?|млн\.?|миллион[аов]*)?\.?", t)
    if not m:
        return None
    number = float(re.sub(r"\s", "", m.group(1)).replace(",", "."))
    unit = m.group(2) or ""
    if unit.startswith(("к", "k", "тыс")):
        number *= 1_000
    elif unit.startswith(("млн", "миллион")):
        number *= 1_000_000
    value = int(round(number))
    return value if 0 < value <= 100_000_000 else None


# ── Распознавание КП ────────────────────────────────────────────────────────

def find_kp(msg: Message) -> dict | None:
    """
    Снимок КП по сообщению. Основной путь — ссылка номера лота, её Telegram
    сохраняет при пересылке. Запасной — номер лота вместе с ценой из текста:
    по одному номеру нельзя, он повторяется между листами.
    """
    text = msg.caption or msg.text or ""
    entities = list(msg.caption_entities or ()) + list(msg.entities or ())
    for ent in entities:
        url = getattr(ent, "url", None) or ""
        m = _TOKEN_RE.search(url)
        if m:
            snap = get_snapshot(m.group(1))
            if snap:
                return snap
    m = _TOKEN_RE.search(text)
    if m and (snap := get_snapshot(m.group(1))):
        return snap

    lot = re.search(r"#(\d{2,6})", text)
    price = re.search(r"(\d[\d\s  ]{4,})\s*(?:руб|₽)", text)
    if lot and price:
        return find_snapshot(lot.group(1), int(re.sub(r"\D", "", price.group(1))))
    return None


# ── Шаги ────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.clear()
    await update.message.reply_text(
        "Здравствуйте! Я превращаю КП Montaro в презентацию от имени вашей компании.\n\n"
        "Перешлите сюда КП по автомобилю — целиком, вместе с фото и подписью.\n\n"
        "Сменить логотип — /logo",
    )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    step = ctx.user_data.get("step")

    if step in ("logo", "logo_only") and (msg.photo or msg.document):
        return await receive_logo(update, ctx)
    if step == "markup" and msg.text:
        return await receive_markup_amount(update, ctx)

    if not (msg.caption or msg.text):
        # альбом приходит пачкой: подпись только у первого фото, остальные молча пропускаем
        if msg.media_group_id:
            return
        if step == "logo":
            await msg.reply_text("Пришлите логотип картинкой или файлом PNG/JPG.")
            return
        await msg.reply_text("Перешлите КП целиком — вместе с подписью, где цена и комплектация.")
        return

    snapshot = find_kp(msg)
    if not snapshot:
        if step == "logo":
            await msg.reply_text("Жду логотип — картинкой или файлом PNG/JPG.")
            return
        await msg.reply_text(
            "Не нашёл это КП. Бот работает только с КП Montaro.\n\n"
            "Перешлите сообщение с КП без изменений — если скопировать текст вручную, "
            "метка КП теряется. Если подпись скрыта при пересылке, включите её.",
        )
        return

    ctx.user_data.clear()
    ctx.user_data["token"] = snapshot["token"]
    deck = presentation.Deck(snapshot)
    title = " ".join(x for x in (deck.make, deck.model.replace("‑", "-"),
                                 str(snapshot["data"].get("year") or "")) if x)
    await msg.reply_text(
        f"✅ КП найдено: <b>{title}</b>\n\nЧто подготовить?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(FORMAT_NAMES["pdf"], callback_data="fmt:pdf"),
             InlineKeyboardButton(FORMAT_NAMES["story"], callback_data="fmt:story")],
            [InlineKeyboardButton(FORMAT_NAMES["both"], callback_data="fmt:both")],
        ]),
    )


async def logo_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/logo — посмотреть или заменить логотип, не пересылая КП."""
    profile = get_client_profile(update.effective_user.id) or {}
    ctx.user_data["step"] = "logo_only"
    text = "Пришлите новый логотип — картинкой или файлом PNG/JPG."
    if profile.get("logo_path") and Path(profile["logo_path"]).exists():
        with open(profile["logo_path"], "rb") as fh:
            await update.message.reply_photo(photo=fh, caption="Сейчас стоит этот логотип.\n\n" + text)
        return
    await update.message.reply_text("Логотипа пока нет.\n\n" + text)


async def on_format(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not ctx.user_data.get("token"):
        await query.edit_message_text("Перешлите КП заново — я его потерял.")
        return
    choice = query.data.split(":")[1]
    ctx.user_data["formats"] = ["pdf", "story"] if choice == "both" else [choice]
    await query.edit_message_text(f"Формат: {FORMAT_NAMES[choice]}")
    await ask_logo(query.message, ctx, update.effective_user.id)


async def ask_logo(message: Message, ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    profile = get_client_profile(user_id) or {}
    ctx.user_data["step"] = "logo"
    if profile.get("logo_path") and Path(profile["logo_path"]).exists():
        with open(profile["logo_path"], "rb") as fh:
            await message.reply_photo(
                photo=fh,
                caption="Ваш логотип. Оставить его?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Оставить", callback_data="logo:keep"),
                     InlineKeyboardButton("🔄 Заменить", callback_data="logo:new")],
                    [InlineKeyboardButton("Без логотипа в этот раз", callback_data="logo:none")],
                ]),
            )
        return
    await message.reply_text(
        "Пришлите логотип вашей компании — он встанет на каждую страницу.\n\n"
        "<i>Лучше всего PNG с прозрачным фоном, отправленный файлом.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Без логотипа", callback_data="logo:none")]]),
    )


async def on_logo_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    if action == "new":
        ctx.user_data["step"] = "logo"
        await query.message.reply_text("Пришлите новый логотип — картинкой или файлом PNG/JPG.")
        return
    ctx.user_data["no_logo"] = action == "none"
    await ask_markup(query.message, ctx, update.effective_user.id)


async def receive_logo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg.document:
        if not (msg.document.mime_type or "").startswith("image/"):
            await msg.reply_text("Это не картинка. Пришлите логотип в PNG или JPG.")
            return
        tg_file = await msg.document.get_file()
    else:
        tg_file = await msg.photo[-1].get_file()

    data = await tg_file.download_as_bytearray()
    try:
        img = Image.open(io.BytesIO(bytes(data)))
        img.load()
    except Exception:
        await msg.reply_text("Не получилось открыть картинку. Пришлите PNG или JPG.")
        return

    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGO_DIR / f"{update.effective_user.id}.png"
    img.convert("RGBA").save(path, "PNG")
    save_client_logo(update.effective_user.id, str(path))
    ctx.user_data["no_logo"] = False

    note = ""
    if min(img.size) < 200:
        note = ("\n\n⚠️ Логотип маленький — в презентации он может выглядеть размыто. "
                "Если есть покрупнее, пришлите его позже через «Заменить».")
    elif img.mode not in ("RGBA", "LA", "P") and msg.photo:
        note = ("\n\n💡 Фото без прозрачного фона ляжет на синий фон прямоугольником. "
                "PNG-файлом будет аккуратнее.")
    await msg.reply_text(f"✅ Логотип сохранён — в следующий раз загружать не нужно.{note}")
    # замена через /logo — сборки нет, дальше спрашивать нечего
    if ctx.user_data.get("step") == "logo_only" or not ctx.user_data.get("token"):
        ctx.user_data.pop("step", None)
        return
    await ask_markup(msg, ctx, update.effective_user.id)


async def ask_markup(message: Message, ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    ctx.user_data["step"] = "markup_choice"
    profile = get_client_profile(user_id) or {}
    rows = [[InlineKeyboardButton("Без наценки", callback_data="mk:none"),
             InlineKeyboardButton("➕ Добавить наценку", callback_data="mk:add")]]
    if profile.get("markup_rub"):
        rows.append([InlineKeyboardButton(f"Как в прошлый раз: {_fmt_rub(profile['markup_rub'])}",
                                          callback_data="mk:last")])
    await message.reply_text(
        "Наценка прибавится к цене. В презентации клиент увидит только итоговую цену.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_markup_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    uid = update.effective_user.id
    if action == "add":
        ctx.user_data["step"] = "markup"
        await query.edit_message_text("Введите наценку в рублях, например: 300 000")
        return
    markup = 0
    if action == "last":
        markup = int((get_client_profile(uid) or {}).get("markup_rub") or 0)
    await query.edit_message_text("Без наценки" if not markup else f"Наценка: {_fmt_rub(markup)}")
    await finish(query.message, ctx, uid, markup)


async def receive_markup_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    markup = parse_markup(msg.text or "")
    if markup is None:
        await msg.reply_text("Не понял сумму. Введите наценку в рублях, например: 500 000 или 500к")
        return
    save_client_markup(update.effective_user.id, markup)
    await finish(msg, ctx, update.effective_user.id, markup)


async def finish(message: Message, ctx: ContextTypes.DEFAULT_TYPE, user_id: int, markup: int) -> None:
    """Создаём задание и предлагаем конструктор фото или сборку сразу."""
    snapshot = get_snapshot(ctx.user_data.get("token", ""))
    if not snapshot:
        await message.reply_text("Перешлите КП заново — я его потерял.")
        return
    layout = presentation.default_layout(len(presentation.snapshot_photos(snapshot)))
    # дизайн — тот, что контрагент выбрал в прошлый раз
    design = (get_client_profile(user_id) or {}).get("design") or "classic"
    layout["_design"] = {"id": design if design in presentation.DESIGNS else "classic"}
    # «Без логотипа» — только для этой презентации, сохранённый логотип не трогаем
    job_id = create_job(user_id, message.chat_id, snapshot["token"],
                        ctx.user_data.get("formats") or ["pdf"], markup, layout,
                        with_logo=not ctx.user_data.get("no_logo"))
    base = int(snapshot["price_rub"])
    price = presentation.rub(base + markup)
    # Контрагенту показываем расчёт — клиенту в файле уйдёт только итоговая цена
    breakdown = (f"Цена в КП: {_fmt_rub(base)}\n"
                 f"Наценка: {_fmt_rub(markup)}\n") if markup else f"Цена в КП: {_fmt_rub(base)}\n"
    ctx.user_data.clear()

    rows = []
    if WEB_APP_URL:
        rows.append([InlineKeyboardButton(
            "🖼 Подставить изображения",
            web_app=WebAppInfo(url=f"{WEB_APP_URL}/client/?job={job_id}"))])
    rows.append([InlineKeyboardButton("⚡ Собрать автоматически", callback_data=f"build:{job_id}")])
    await message.reply_text(
        f"{breakdown}Цена в презентации: <b>{price}</b>\n\n"
        "Откройте конструктор, чтобы выбрать, какие фото и куда поставить, — "
        "или соберу автоматически.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_build(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await deliver_job(ctx.bot, query.data.split(":", 1)[1])


async def _post_init(app: Application) -> None:
    # Меню команд задаём при запуске: после перевыпуска токена оно не потеряется
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "Как пользоваться"),
        BotCommand("logo", "Посмотреть или заменить логотип"),
    ])


def main() -> None:
    if not TOKEN:
        raise SystemExit("CLIENT_BOT_TOKEN не задан — клиентский бот не запущен")
    init_db()
    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("logo", logo_command))
    app.add_handler(CallbackQueryHandler(on_format, pattern=r"^fmt:"))
    app.add_handler(CallbackQueryHandler(on_logo_button, pattern=r"^logo:"))
    app.add_handler(CallbackQueryHandler(on_markup_button, pattern=r"^mk:"))
    app.add_handler(CallbackQueryHandler(on_build, pattern=r"^build:"))
    app.add_handler(MessageHandler(~filters.COMMAND, on_message))
    log.info("Клиентский бот запущен")
    # Python 3.14: get_event_loop() больше не создаёт цикл сам — как в bot.py
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
