"""
Сборка и отправка КП в Telegram — общее для бота и веб-API.
"""
import logging

from telegram import InputMediaPhoto

from ai import build_options
from calc import total_rub
from kp import brand_candidates, build_kp_parts
from storage import get_brand_emoji, get_float, get_optional

CONTACT = "@Aleksandr_Montaro"

# Опции берём все, что есть у машины: сколько поместится — решит лимит подписи

log = logging.getLogger("autokp.kp")


def _telegram_photo(url: str) -> str:
    """
    Telegram не принимает webp как фотографию. autoscout24 отдаёт превью
    в webp — подменяем на jpeg. Нужно и для старых записей в истории,
    сохранённых до правки парсера.
    """
    if url.endswith(".webp") and "autoscout24" in url:
        return url[: -len(".webp")] + ".jpg"
    return url


def pick_photos(all_photos: list[str]) -> list[str]:
    """
    Фото для КП с шагом: подряд идущие кадры — это почти всегда экстерьер,
    поэтому берём каждое N-е, чтобы в подборку попал и салон.
    Если фото в объявлении мало — добираем оставшиеся подряд.
    """
    if not all_photos:
        return []
    offset = int(get_float("img_offset", 0))
    count  = int(get_float("img_count", 6)) or 6
    step   = max(1, int(get_float("img_step", 2)))

    idxs = list(range(offset, len(all_photos), step))[:count]
    if len(idxs) < count:
        for i in range(offset, len(all_photos)):
            if i not in idxs:
                idxs.append(i)
            if len(idxs) >= count:
                break
        idxs.sort()
    return [all_photos[i] for i in idxs]


async def build_captions(
    d: dict, car_num: int | str, contact: str = CONTACT
) -> tuple[list[str], list[str]]:
    """
    Текст КП частями: одна — если всё влезло в подпись к фото, иначе две.
    Возвращает (с премиум-эмодзи, без них) — второй набор нужен как запасной,
    если Telegram откажется принять custom emoji.
    """
    options = await build_options(d)
    total   = total_rub(d)

    # Сначала пробуем составную марку («Alfa Romeo»), потом одно слово
    title_for_brand = d.get("title") or f"{d.get('make', '')} {d.get('model', '')}"
    rec = None
    for cand in brand_candidates(title_for_brand):
        rec = get_brand_emoji(cand)
        if rec:
            break
    fallback = (rec or {}).get("emoji") or "🚗"
    price_emoji_id  = get_optional("price_emoji_id")
    header_emoji_id = get_optional("header_emoji_id")
    header_fallback = get_optional("header_emoji") or "🇪🇺"

    with_emoji = build_kp_parts(
        d, total, options, car_num, contact,
        brand_emoji_id=(rec or {}).get("custom_emoji_id"),
        brand_emoji_fallback=fallback,
        price_emoji_id=price_emoji_id,
        header_emoji_id=header_emoji_id,
        header_emoji_fallback=header_fallback,
    )
    plain = build_kp_parts(
        d, total, options, car_num, contact,
        brand_emoji_fallback=fallback,
        header_emoji_fallback=header_fallback,
    )
    return with_emoji, plain


async def send_kp(
    bot,
    chat_id: int,
    car_num: int | str,
    d: dict,
    photos: list[str] | None = None,
    contact: str = CONTACT,
) -> dict | None:
    """
    Отправляет КП: текст подписью к первому фото + альбом.
    photos=None — выбрать автоматически (с шагом); иначе берём переданный список
    (так приходит выбор из мини-аппа).

    Комплектация может не влезть в подпись — тогда её хвост вместе с ценой
    и контактами уходит следующим сообщением.
    Возвращает данные для последующей замены фото или None, если фото не было.
    """
    parts, parts_plain = await build_captions(d, car_num, contact)
    caption, caption_plain = parts[0], parts_plain[0]
    rest, rest_plain = parts[1:], parts_plain[1:]

    all_photos = d.get("photos", []) or []
    chosen     = [_telegram_photo(u) for u in (photos or pick_photos(all_photos))]

    async def _send_rest(texts: list[str]) -> None:
        """Продолжение КП отдельными сообщениями — с тем же запасным вариантом."""
        for i, text in enumerate(texts):
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            except Exception as exc:
                log.warning("Продолжение КП не ушло (%s), пробую без премиум-эмодзи", exc)
                await bot.send_message(
                    chat_id=chat_id, text=rest_plain[i], parse_mode="HTML"
                )

    async def _send(text: str) -> list[int]:
        if len(chosen) == 1:
            msg = await bot.send_photo(
                chat_id=chat_id, photo=chosen[0], caption=text, parse_mode="HTML"
            )
            return [msg.message_id]
        media = [InputMediaPhoto(media=url) for url in chosen]
        media[0] = InputMediaPhoto(media=chosen[0], caption=text, parse_mode="HTML")
        msgs = await bot.send_media_group(chat_id=chat_id, media=media)
        return [m.message_id for m in msgs]

    if not chosen:
        try:
            await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")
        except Exception:
            await bot.send_message(chat_id=chat_id, text=caption_plain, parse_mode="HTML")
        await _send_rest(rest)
        return None

    try:
        photo_msg_ids = await _send(caption)
    except Exception as first:
        # Чаще всего причина — премиум-эмодзи: повторяем обычным текстом
        log.warning("КП с премиум-эмодзи не ушло (%s), пробую обычным текстом", first)
        try:
            caption = caption_plain
            photo_msg_ids = await _send(caption)
        except Exception as second:
            log.warning("Альбом не ушёл (%s), пробую одним фото", second)
            msg = await bot.send_photo(
                chat_id=chat_id, photo=chosen[0], caption=caption, parse_mode="HTML"
            )
            photo_msg_ids = [msg.message_id]

    await _send_rest(rest)

    return {
        "chat_id":       chat_id,
        "photo_msg_ids": photo_msg_ids,
        "used_photos":   list(chosen),      # показаны сейчас (меняется при замене)
        "shown_photos":  set(chosen),       # все, что вообще показывались
        "all_photos":    all_photos,        # вся галерея
        "caption":       caption,           # сохраняем при замене первого фото
        "caption_idx":   0,                 # какое фото несёт подпись
    }
