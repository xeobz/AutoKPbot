"""
API конструктора презентации (мини-апп клиентского бота).

Подпись initData проверяется токеном клиентского бота, а не KP_bot: мини-апп
открывается из другого бота. Превью, фото и логотип отдаются по id задания —
он случайный и длинный, а <img> и <iframe> не умеют слать заголовки.
"""
import asyncio
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from PIL import Image
from pydantic import BaseModel

import presentation
from storage import (get_client_profile, get_job, get_setting, get_snapshot,
                     set_job_status, update_job_layout)

log = logging.getLogger("autokp.client_api")
router = APIRouter()

CLIENT_TOKEN = os.getenv("CLIENT_BOT_TOKEN", "")
DEV_MODE = os.getenv("WEB_DEV_MODE", "") == "1"
DEV_USER_ID = int(os.getenv("WEB_DEV_USER_ID", "0") or 0)

_bot = None


def client_bot():
    global _bot
    if _bot is None and CLIENT_TOKEN:
        from telegram import Bot
        _bot = Bot(CLIENT_TOKEN)
    return _bot


async def client_user(x_init_data: str = Header(default="", alias="X-Init-Data")) -> int:
    from webapi import check_init_data
    if x_init_data:
        user = check_init_data(x_init_data, token=CLIENT_TOKEN)
        if not user:
            raise HTTPException(401, "Подпись Telegram не сошлась")
        return int(user["id"])
    if DEV_MODE and DEV_USER_ID:
        return DEV_USER_ID
    raise HTTPException(401, "Откройте конструктор через бота")


def _job_or_404(job_id: str) -> tuple[dict, dict]:
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Задание не найдено — перешлите КП боту заново")
    snapshot = get_snapshot(job["token"])
    if not snapshot:
        raise HTTPException(404, "КП больше не найдено")
    return job, snapshot


def _owned(job_id: str, user_id: int) -> tuple[dict, dict]:
    job, snapshot = _job_or_404(job_id)
    if job["user_id"] != user_id:
        raise HTTPException(403, "Это задание другого пользователя")
    return job, snapshot


def _delivery(snapshot: dict) -> str:
    key = "kp_delivery_msk" if snapshot.get("direction") == "msk" else "kp_delivery"
    return get_setting(key) or ""


@router.get("/jobs/{job_id}")
async def job_info(job_id: str, user_id: int = Depends(client_user)):
    job, snapshot = _owned(job_id, user_id)
    photos = presentation.snapshot_photos(snapshot)
    deck = presentation.Deck(snapshot, job["markup_rub"])
    return {
        "id": job_id,
        "status": job["status"],
        "formats": job["formats"],
        "title": " ".join(x for x in (deck.make, deck.model.replace("‑", "-")) if x),
        "price": presentation.rub(deck.price),
        "layout": job["layout"],
        "slots": {fmt: [{"name": n, "title": presentation.SLOT_TITLES[n]} for n, _ in slots]
                  for fmt, slots in presentation.SLOTS.items() if fmt in job["formats"]},
        "photos": [{"idx": i, "thumb": f"/api/client/jobs/{job_id}/photo/{i}?thumb=1"}
                   for i in range(len(photos))],
    }


@router.get("/jobs/{job_id}/photo/{idx}")
async def job_photo(job_id: str, idx: int, thumb: int = 0):
    _, snapshot = _job_or_404(job_id)
    photos = presentation.snapshot_photos(snapshot)
    if not 0 <= idx < len(photos):
        raise HTTPException(404, "Нет такого фото")
    path = await presentation.cached_photo(photos[idx])
    if not path:
        raise HTTPException(502, "Фото не загрузилось с сайта объявления")
    if thumb:
        small = path.with_name(path.stem + "_t.jpg")
        if not small.exists():
            def make_thumb():
                img = Image.open(path)
                img.thumbnail((420, 420))
                img.save(small, "JPEG", quality=82)
            await asyncio.to_thread(make_thumb)
        path = small
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/jobs/{job_id}/logo")
async def job_logo(job_id: str):
    job, _ = _job_or_404(job_id)
    profile = get_client_profile(job["user_id"]) or {}
    path = profile.get("logo_path") or ""
    if not path or not os.path.exists(path):
        raise HTTPException(404, "Логотипа нет")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@router.get("/font")
async def font():
    return FileResponse(presentation.FONT_FILE, media_type="font/ttf",
                        headers={"Cache-Control": "public, max-age=2592000"})


# Подсказки только для превью — в файл не попадают. Макет в мини-аппе сильно
# уменьшен (PDF примерно в 4 раза), поэтому размеры значка заданы крупно.
def preview_css(fmt: str) -> str:
    size = 44 if fmt == "pdf" else 34
    return f"""<style>
.slot{{cursor:pointer;touch-action:pan-y}}
.slot.sel{{touch-action:none;cursor:grab;outline:{size // 5}px solid #6fc0ff;outline-offset:-{size // 5}px;z-index:5}}
.slot::after{{content:"✎ Заменить";position:absolute;left:{size // 2}px;top:{size // 2}px;z-index:6;
 font:700 {size}px/1 Manrope,Arial,sans-serif;color:#fff;background:rgba(4,18,46,.72);
 border:{max(2, size // 16)}px solid rgba(111,192,255,.8);border-radius:999px;padding:{size // 3}px {size // 2}px;
 pointer-events:none;backdrop-filter:blur(6px)}}
.slot.sel::after{{content:"Двигайте пальцем";background:#2f8bff;border-color:#2f8bff}}
</style>"""


@router.get("/jobs/{job_id}/preview/{fmt}")
async def job_preview(job_id: str, fmt: str):
    job, snapshot = _job_or_404(job_id)
    if fmt not in ("pdf", "story") or fmt not in job["formats"]:
        raise HTTPException(404, "Такого формата в задании нет")
    profile = get_client_profile(job["user_id"]) or {}
    logo = (f"/api/client/jobs/{job_id}/logo"
            if profile.get("logo_path") and job.get("with_logo", 1) else "")
    deck = presentation.Deck(snapshot, job["markup_rub"], logo,
                             get_setting("kp_country") or "", _delivery(snapshot),
                             font_url="/api/client/font")

    def src(i: int) -> str:
        return f"/api/client/jobs/{job_id}/photo/{i}"

    page = deck.pdf_html(job["layout"], src) if fmt == "pdf" else deck.story_html(job["layout"], src)
    return HTMLResponse(page.replace("</head>", preview_css(fmt) + "</head>", 1),
                        headers={"Cache-Control": "no-store"})


class SubmitReq(BaseModel):
    layout: dict


def _clean_layout(raw: dict, photo_count: int) -> dict:
    """Раскладку из браузера не доверяем: только известные слоты и допустимые числа."""
    known = {n for slots in presentation.SLOTS.values() for n, _ in slots}
    out = {}
    for name, s in (raw or {}).items():
        if name not in known or not isinstance(s, dict):
            continue
        try:
            photo = int(s.get("photo"))
        except (TypeError, ValueError):
            continue
        if not 0 <= photo < photo_count:
            continue
        out[name] = {
            "photo": photo,
            "zoom": max(1.0, min(3.0, float(s.get("zoom", 1) or 1))),
            "x": max(0.0, min(100.0, float(s.get("x", 50) or 0))),
            "y": max(0.0, min(100.0, float(s.get("y", 50) or 0))),
        }
    return out


@router.post("/jobs/{job_id}/submit")
async def job_submit(job_id: str, req: SubmitReq, tasks: BackgroundTasks,
                     user_id: int = Depends(client_user)):
    job, snapshot = _owned(job_id, user_id)
    if job["status"] == "rendering":
        raise HTTPException(409, "Презентация уже собирается — файлы скоро придут в чат")
    bot = client_bot()
    if not bot:
        raise HTTPException(503, "Клиентский бот не настроен")
    layout = _clean_layout(req.layout, len(presentation.snapshot_photos(snapshot)))
    update_job_layout(job_id, layout)
    set_job_status(job_id, "queued")

    from clientsend import deliver_job
    tasks.add_task(deliver_job, bot, job_id)
    return {"ok": True}
