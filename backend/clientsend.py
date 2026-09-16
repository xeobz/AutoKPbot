"""
Сборка и отправка презентации контрагенту — общее для клиентского бота
(кнопка «Собрать сразу») и веб-API (кнопка «Сохранить» в конструкторе).
"""
import logging
import shutil
from pathlib import Path

from telegram import Bot, InputFile

import presentation
from storage import get_client_profile, get_job, get_setting, get_snapshot, set_job_status

log = logging.getLogger("autokp.client")


async def deliver_job(bot: Bot, job_id: str) -> bool:
    """
    Собирает файлы по сохранённой раскладке и шлёт их в чат контрагента.
    Возвращает True, если всё ушло. Ошибку пишет в задание и сообщает человеку.
    """
    job = get_job(job_id)
    if not job:
        return False
    snapshot = get_snapshot(job["token"])
    if not snapshot:
        await bot.send_message(job["chat_id"], "❌ КП больше не найдено. Перешлите его заново.")
        set_job_status(job_id, "error")
        return False

    profile = get_client_profile(job["user_id"]) or {}
    set_job_status(job_id, "rendering")
    status = await bot.send_message(job["chat_id"], "⏳ Собираю презентацию… Обычно это до минуты.")

    files: dict[str, Path] = {}
    try:
        files = await presentation.render(
            snapshot, job["formats"], job["layout"],
            markup_rub=job["markup_rub"],
            logo_path=(profile.get("logo_path") or "") if job.get("with_logo", 1) else "",
            country=get_setting("kp_country") or "",
            delivery=_delivery(snapshot),
        )
        if "story" in files:
            with open(files["story"], "rb") as fh:
                # документом, а не фото: Telegram пережимает фото и мылит текст
                await bot.send_document(job["chat_id"], InputFile(fh, filename=files["story"].name),
                                        caption="📱 Картинка для сторис")
        if "pdf" in files:
            with open(files["pdf"], "rb") as fh:
                await bot.send_document(job["chat_id"], InputFile(fh, filename=files["pdf"].name),
                                        caption="📄 Презентация PDF")
        set_job_status(job_id, "done")
        await status.delete()
        return True
    except Exception as exc:
        log.exception("Презентация %s не собралась: %s", job_id, exc)
        set_job_status(job_id, "error")
        await status.edit_text("❌ Не получилось собрать файл. Попробуйте ещё раз через минуту.")
        return False
    finally:
        for path in files.values():
            shutil.rmtree(path.parent, ignore_errors=True)
            break


def _delivery(snapshot: dict) -> str:
    key = "kp_delivery_msk" if snapshot.get("direction") == "msk" else "kp_delivery"
    return get_setting(key) or ""
