"""
Презентация КП для контрагента: PDF (16:9) и картинка для сторис (1080×1920).

Строится из снимка отправленного КП (storage.get_snapshot): полная комплектация,
все поля объявления, фото в исходном размере. Одна и та же разметка идёт
и в конструктор мини-аппа, и в Chrome на печать — что контрагент видит
в превью, то и получит файлом.

Правило для всего модуля: нет данных — нет элемента. Ни «Мощность: —»,
ни пустой рамки под фото, ни страницы без содержимого.
"""
import asyncio
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from PIL import Image

log = logging.getLogger("autokp.presentation")

E = html.escape
MEDIA_DIR = Path(__file__).parent / "client_media"
PHOTO_CACHE = MEDIA_DIR / "photos"
CHROME = os.getenv("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium") or \
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Chrome на одном ядре: презентации собираем строго по одной
_render_lock = asyncio.Lock()


# ── Данные ──────────────────────────────────────────────────────────────────

def split_title(title: str) -> tuple[str, str, str]:
    """
    «Volkswagen Tiguan R-Line 2.0 TDI DSG Navi Kamera ACC SHZ» →
    («Volkswagen», «Tiguan R-Line», «2.0 TDI»). Всё после двигателя — сокращения
    продавца для поиска, в презентации им не место.
    """
    title = re.split(r"[*|•!,]", title or "", maxsplit=1)[0].strip()
    m = re.search(r"\b\d[.,]\d\s*(?:TDI|TSI|TFSI|CDI|BlueHDi|d|i|T)?\b", title)
    head = title[: m.end()] if m else title
    words = head.split()
    if not words:
        return "", "", ""
    make = words[0]
    engine = m.group(0).replace(",", ".") if m else ""
    model = " ".join(words[1:])
    if engine:
        model = model.replace(m.group(0), "").strip()
    # Составные марки: Mercedes-Benz одним словом, а Land Rover / Alfa Romeo — двумя
    if make.lower() in ("land", "alfa", "aston") and model:
        extra, _, model = model.partition(" ")
        make = f"{make} {extra}"
    return make, model.replace("-", "\u2011"), engine


def _gearbox(raw: str) -> str:
    return {"автомат": "Автоматическая", "механика": "Механическая"}.get((raw or "").lower(), raw or "")


def _num(v) -> str:
    return f"{int(v):,}".replace(",", " ")


def cover_specs(d: dict) -> list[tuple[str, str, str]]:
    """Обложка: главные цифры. Пустые не выводятся."""
    out = []
    if d.get("year"):
        out.append(("calendar", "Год", str(d["year"])))
    if d.get("mileage") == 0:
        out.append(("gauge", "Пробег", "Новый"))
    elif d.get("mileage"):
        out.append(("gauge", "Пробег", f"{_num(d['mileage'])} км"))
    if d.get("power_hp"):
        out.append(("bolt", "Мощность", f"{d['power_hp']} л.с."))
    if d.get("gearbox"):
        out.append(("gear", "Коробка", _gearbox(d["gearbox"])))
    if d.get("drive"):
        out.append(("drive", "Привод", d["drive"]))
    return out


def detail_specs(d: dict, country: str = "", delivery: str = "") -> list[tuple[str, str, str]]:
    """Страница «Об автомобиле» — только то, чего нет на обложке."""
    a = {k: str(v).strip() for k, v in (d.get("attrs") or {}).items() if str(v).strip()}
    out: list[tuple[str, str, str]] = []

    if a.get("firstRegistration"):
        out.append(("calendar", "Первая регистрация", a["firstRegistration"].replace("/", ".")))
    body = (a.get("category") or "").split("/")[0].strip()
    if body:
        out.append(("car", "Кузов", body))

    bits = []
    ccm = re.sub(r"\D", "", a.get("cubicCapacity", ""))
    if ccm:
        bits.append(f"{_num(ccm)} см³")
    elif d.get("engine_l"):
        bits.append(f"{d['engine_l']} л")
    if a.get("cylinder"):
        bits.append(f"{a['cylinder']} цил.")
    if bits:
        out.append(("engine", "Двигатель", " · ".join(bits)))
    if d.get("fuel"):
        out.append(("fuel", "Топливо", str(d["fuel"]).capitalize()))

    color = str(d.get("color") or "").capitalize()
    factory = a.get("manufacturerColorName", "")
    if color or factory:
        out.append(("paint", "Цвет кузова", " · ".join(x for x in (color, factory) if x)))
    if a.get("interior"):
        out.append(("seat", "Салон", a["interior"]))

    seats, doors = a.get("numSeats", ""), a.get("doorCount", "")
    if seats or doors:
        out.append(("users", "Мест и дверей",
                    " · ".join(x for x in (f"{seats} мест" if seats else "",
                                           f"{doors} дверей" if doors else "") if x)))
    if a.get("numberOfPreviousOwners"):
        out.append(("user", "Владельцев", a["numberOfPreviousOwners"]))
    dmg = a.get("damageCondition", "")
    if dmg:
        out.append(("shield", "Состояние", dmg.split(", ")[-1]))
    if a.get("climatisation"):
        out.append(("snow", "Климат", a["climatisation"].replace("Климат-контроль ", "")))
    if a.get("emissionClass"):
        out.append(("leaf", "Экокласс", a["emissionClass"]))
    if a.get("hu"):
        out.append(("check", "Техосмотр", {"Новые": "Новый"}.get(a["hu"], a["hu"])))
    if country:
        out.append(("globe", "Страна", country))
    if delivery:
        out.append(("truck", "Срок доставки", delivery))
    return out


CATEGORIES = [
    ("light", "Экстерьер и свет", ("фар", "фонар", "дальнего света", "свет для", "диск", "тонир",
                                  "рейлинг", "фаркоп", "наружн", "бампер", "кузов")),
    ("screen", "Мультимедиа", ("carplay", "android", "app-connect", "навиг", "мультимед", "аудио",
                               "harman", "burmester", "bang", "dab", "usb", "bluetooth", "телефон",
                               "зарядк", "дисплей", "приборн", "голос", "cockpit", "radio", "розетк",
                               "проекц", "head-up")),
    ("drive", "Динамика и шасси", ("подвеск", "привод", "рулевое управ", "спортивный пакет",
                                   "профиля движения", "спуска", "дифференц", "тормозн")),
    ("shield", "Безопасность и ассистенты", ("ассистент", "assist", "круиз", "acc", "камер",
                                            "парков", "тормож", "полос", "слеп", "датчик",
                                            "распознав", "усталост", "защит", "pre-safe")),
    ("wheel", "Салон", ("обивк", "отделк", "руль", "рулевое колесо", "потолок", "амбиент",
                        "подсвет", "кож", "alcantara", "накладк", "коврик", "вставк", "педал",
                        "подлокот", "поясн", "nappa")),
    ("seat", "Комфорт", ("подогрев", "обогрев", "вентиляц", "отопит", "климат", "багаж", "cargo",
                         "люк", "keyless", "бесключ", "затемн", "зеркал", "память", "массаж",
                         "панорам", "электро", "сиден", "спинк")),
]
KEY_PRIORITY = ("панорам", "проекц", "head-up", "матрич", "multibeam", "iq.light", "led",
                "burmester", "harman", "bang", "камер", "круиз", "acc", "массаж", "вентиляц",
                "пневмо", "airmatic", "app-connect", "carplay", "навиг", "отопит", "keyless",
                "бесключ", "подогрев", "парков", "диск", "обивк", "кож")


def categorize(options: list[str]) -> list[tuple[str, str, list[str]]]:
    buckets: dict[str, list[str]] = {k: [] for k, _, _ in CATEGORIES}
    other: list[str] = []
    for opt in options:
        low = opt.lower()
        for key, _, words in CATEGORIES:
            if any(w in low for w in words):
                buckets[key].append(opt)
                break
        else:
            other.append(opt)
    out = [(k, t, buckets[k]) for k, t, _ in CATEGORIES if buckets[k]]
    if other:
        out.append(("plus", "Другие опции", other))
    return out


def key_options(options: list[str], n: int) -> list[str]:
    picked: list[str] = []
    for word in KEY_PRIORITY:
        for opt in options:
            if word in opt.lower() and opt not in picked:
                picked.append(opt)
                break
        if len(picked) >= n:
            return picked
    for opt in options:
        if len(picked) >= n:
            break
        if opt not in picked:
            picked.append(opt)
    return picked


def rub(v: int) -> str:
    return f"{_num(v)} ₽"


# ── Слоты фото ──────────────────────────────────────────────────────────────
# Имя слота → (формат, порядковый номер фото по умолчанию). Конструктор в мини-аппе
# меняет фото, масштаб и сдвиг; рендер берёт ровно то, что сохранено.
SLOTS = {
    "pdf": [("cover", 0), ("gallery_main", 1), ("gallery_a", 3), ("gallery_b", 2),
            ("key_top", 5), ("key_bottom", 6)],
    "story": [("story_main", 0), ("story_a", 3), ("story_b", 1)],
}
SLOT_TITLES = {
    "cover": "Обложка", "gallery_main": "Галерея — крупное", "gallery_a": "Галерея — слева",
    "gallery_b": "Галерея — справа", "key_top": "Опции — верхнее", "key_bottom": "Опции — нижнее",
    "story_main": "Сторис — крупное", "story_a": "Сторис — слева", "story_b": "Сторис — справа",
}


def default_layout(photo_count: int) -> dict:
    """Раскладка по умолчанию. Фото не хватает — слот просто не заполняется."""
    layout = {}
    for slots in SLOTS.values():
        for name, idx in slots:
            if photo_count == 0:
                continue
            # фото меньше, чем слотов: берём по кругу, но не повторяем в одном блоке
            layout[name] = {"photo": idx if idx < photo_count else None, "zoom": 1.0, "x": 50, "y": 50}
    return {k: v for k, v in layout.items() if v["photo"] is not None}


def _slot(name: str, layout: dict, src, extra_class: str = "") -> str:
    """Рамка с фото. Слот без фото не выводится вовсе."""
    s = layout.get(name)
    if not s or s.get("photo") is None:
        return ""
    zoom = max(1.0, min(3.0, float(s.get("zoom", 1))))
    x = max(0.0, min(100.0, float(s.get("x", 50))))
    y = max(0.0, min(100.0, float(s.get("y", 50))))
    style = (f"object-position:{x:.1f}% {y:.1f}%;transform:scale({zoom:.3f});"
             f"transform-origin:{x:.1f}% {y:.1f}%")
    return (f'<div class="slot {extra_class}" data-slot="{name}">'
            f'<img src="{E(src(int(s["photo"])))}" style="{style}" draggable="false"></div>')


# ── Разметка ────────────────────────────────────────────────────────────────

ICONS = {
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    "gauge": '<path d="M12 14l4-4"/><path d="M4 18a9 9 0 1 1 16 0"/>',
    "engine": '<path d="M4 10h3l2-3h6v3h3v3h2v4h-2v2H9l-2-2H4z"/>',
    "bolt": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "fuel": '<path d="M4 21V5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v16M3 21h13M15 9h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0V8l-3-3"/>',
    "drive": '<circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="18" r="2"/><path d="M12 6v12M8 6h8M8 18h8"/>',
    "gear": '<circle cx="6" cy="5" r="2"/><circle cx="12" cy="5" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="12" cy="19" r="2"/><path d="M6 7v10M12 7v10M18 7v5H6"/>',
    "paint": '<path d="M12 3c3 4 6 7 6 11a6 6 0 0 1-12 0c0-4 3-7 6-11z"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "truck": '<path d="M2 6h11v10H2zM13 10h4l3 3v3h-7"/><circle cx="6" cy="18" r="2"/><circle cx="17" cy="18" r="2"/>',
    "light": '<path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2z"/>',
    "screen": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "seat": '<path d="M7 3h5l1 9h5l1 5H8z"/><path d="M8 17l-1 4M17 17l1 4"/>',
    "wheel": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2"/><path d="M12 14v7M10 12H3M14 12h7"/>',
    "plus": '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    "check": '<path d="M5 12l5 5L20 7"/>',
    "car": '<path d="M3 13l2-5a2 2 0 0 1 2-1h10a2 2 0 0 1 2 1l2 5v5h-3M3 13v5h3m-3-5h18M6 18h12"/><circle cx="7.5" cy="16.5" r="1.5"/><circle cx="16.5" cy="16.5" r="1.5"/>',
    "users": '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M16 5a3 3 0 0 1 0 6M18 20a6 6 0 0 0-3-5"/>',
    "user": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
    "snow": '<path d="M12 2v20M4 7l16 10M20 7L4 17"/>',
    "leaf": '<path d="M20 4C10 4 4 9 4 16v4h4c7 0 12-6 12-16z"/><path d="M8 16c3-4 6-6 9-7"/>',
}


def icon(name: str) -> str:
    return (f'<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
            f'stroke-linecap="round" stroke-linejoin="round">{ICONS.get(name, ICONS["plus"])}</svg>')


FONTS = ('<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;500;600;700;800'
         '&display=block" rel="stylesheet">')

BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}
:root{--panel:rgba(18,58,122,.42);--panel2:rgba(14,44,96,.55);--line:rgba(96,164,255,.26);
--ink:#eaf2ff;--mute:#8fa6c9;--acc:#2f8bff;--acc2:#6fc0ff}
html,body{background:#04122e;color:var(--ink);font-family:Manrope,Arial,sans-serif}
.bg{background:radial-gradient(1300px 780px at 88% -10%,#1b4f9e 0%,rgba(10,35,80,0) 62%),
 radial-gradient(1000px 700px at -10% 108%,#0d3a7d 0%,rgba(6,20,48,0) 58%),
 linear-gradient(160deg,#04122e 0%,#061a3d 48%,#020a1c 100%)}
.slot{overflow:hidden;position:relative;background:#0a1e44}
.slot img{width:100%;height:100%;object-fit:cover;display:block;user-select:none;-webkit-user-drag:none}
.logo{display:block;object-fit:contain;object-position:left center}
.ic{width:24px;height:24px;flex:none;color:var(--acc)}
.h-make{font-weight:300;letter-spacing:.34em;text-transform:uppercase;color:var(--acc2)}
.h-model{font-weight:800;line-height:1.02;letter-spacing:-.02em}
.chips{display:flex;gap:10px;flex-wrap:wrap}
.chip{border:1px solid var(--line);border-radius:999px;font-weight:600;color:var(--acc2);background:rgba(20,62,130,.35)}
.price-label{letter-spacing:.3em;text-transform:uppercase;color:var(--mute)}
.price{font-weight:800;line-height:1;color:#fff;text-shadow:0 0 46px rgba(47,139,255,.5)}
.price-note{color:var(--mute)}
.spec-card{border:1px solid var(--line);background:var(--panel)}
.spec-card .lbl{letter-spacing:.16em;text-transform:uppercase;color:var(--mute)}
.eyebrow{font-size:13px;letter-spacing:.34em;text-transform:uppercase;color:var(--acc2)}
.section-title{font-size:46px;font-weight:800;line-height:1.06;margin-top:12px;letter-spacing:-.015em}
"""

PDF_CSS = """
@page{size:1600px 900px;margin:0}
.page{width:1600px;height:900px;position:relative;overflow:hidden;page-break-after:always;padding:60px 76px}
.page:last-child{page-break-after:auto}
.topbar{display:flex;justify-content:space-between;align-items:center;height:48px}
.topbar .logo{height:48px;max-width:300px}
.pageno{font-size:12.5px;letter-spacing:.32em;color:var(--mute);text-transform:uppercase;margin-left:auto}
.h-make{font-size:17px}.h-model{font-size:74px;margin-top:12px}
.chips{margin-top:20px}.chip{padding:8px 18px;font-size:15px}
.price-label{font-size:13px}.price{font-size:66px;margin-top:8px}.price-note{font-size:14.5px;margin-top:10px}
.spec-card{border-radius:16px;padding:16px 18px;display:flex;gap:13px;align-items:center}
.spec-card .lbl{font-size:11.5px}.spec-card .val{font-size:19.5px;font-weight:600;margin-top:3px}
.cover .cover-photo{position:absolute;right:0;top:0;width:980px;height:616px;border-radius:0 0 0 34px;
 box-shadow:0 34px 90px rgba(0,0,0,.6)}
.cover .left{position:absolute;left:76px;top:150px;width:600px}
.cover.no-photo .left{width:1100px}
.cover .accent{height:3px;width:110px;background:linear-gradient(90deg,var(--acc),transparent);margin:30px 0}
.cover .strip{position:absolute;left:76px;right:76px;bottom:52px;display:flex;gap:14px}
.cover .strip .spec-card{flex:1}
.p2 .wrap{position:absolute;left:76px;right:76px;top:150px;bottom:56px;display:grid;
 grid-template-columns:700px 1fr;gap:44px}
.p2.no-gallery .wrap{grid-template-columns:1fr;align-content:center}
.p2.no-gallery .detail-grid .spec-card{padding:22px 24px}.p2.no-gallery .detail-grid .val{font-size:22px}
.gallery{display:grid;grid-template-rows:440px 216px;gap:16px;align-content:start}
.gallery.single{grid-template-rows:440px}
.gallery .row{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
.gallery .row.one{grid-template-columns:1fr}
.gallery .slot{border-radius:18px;height:100%}
.detail-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:26px}
.p2.no-gallery .detail-grid{grid-template-columns:repeat(3,1fr)}
.detail-grid .spec-card{padding:14px 16px}.detail-grid .val{font-size:18px}
.p3 .wrap{position:absolute;left:76px;right:76px;top:140px;bottom:56px;display:grid;
 grid-template-columns:1fr 520px;gap:44px}
.p3.no-photos .wrap{grid-template-columns:1fr;align-content:center}
.p3.no-photos .key-grid{grid-template-columns:repeat(3,1fr)}
.key-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:13px;margin-top:26px}
.key-grid .group{border:1px solid var(--line);border-radius:16px;background:var(--panel);
 padding:18px 20px;display:flex;gap:14px;align-items:flex-start;min-height:92px}
.key-grid .c{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--mute);margin-bottom:5px}
.key-grid .t{font-size:18px;font-weight:600;line-height:1.3}
.p3 .photos{display:grid;gap:14px;align-content:start}
.p3 .photos .slot{border-radius:22px;height:330px}
.p4 .cols{position:absolute;left:76px;right:76px;top:232px;bottom:46px;display:grid;
 grid-template-columns:repeat(3,1fr);gap:22px}
.p4 .col{overflow:hidden;min-height:0}
.p4 .group{border:1px solid var(--line);border-radius:14px;background:var(--panel2);padding:13px 18px;margin-bottom:10px}
.p4 .group h4{display:flex;gap:10px;align-items:center;font-size:13px;letter-spacing:.14em;
 text-transform:uppercase;margin-bottom:8px;color:var(--acc2)}
.p4 .group li{list-style:none;font-size:var(--opt-size,13.8px);line-height:1.36;padding:3px 0 3px 18px;position:relative}
.p4 .group li:before{content:"";position:absolute;left:2px;top:13px;width:8px;height:2px;background:var(--acc)}
"""

STORY_CSS = """
@page{size:1080px 1920px;margin:0}
html,body{width:1080px;height:1920px;overflow:hidden}
.story{width:1080px;height:1920px;position:relative;overflow:hidden;padding:64px 64px 58px;display:flex;flex-direction:column}
.c-top{display:flex;justify-content:space-between;align-items:center;min-height:56px}
.c-top .logo{height:56px;max-width:340px}
.c-top .tag{font-size:17px;letter-spacing:.26em;text-transform:uppercase;color:var(--mute);margin-left:auto}
.h-make{font-size:22px;margin-top:46px}.h-model{font-size:88px;margin-top:14px}
.chips{margin-top:22px}.chip{padding:9px 20px;font-size:20px}
.collage{margin:24px -64px 0;display:grid;grid-template-rows:540px 262px;gap:6px}
.collage.single{grid-template-rows:640px}
.collage .row{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.collage .row.one{grid-template-columns:1fr}
.c-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:11px;margin-top:24px}
.c-grid .spec-card{border-radius:14px;padding:13px 14px;display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center}
.c-grid .lbl{font-size:14px}.c-grid .val{font-size:21px;font-weight:700}
.c-grid .ic{width:28px;height:28px}
.c-opts{margin-top:20px;display:grid;grid-template-columns:1fr 1fr;gap:9px 24px}
.c-opts div{display:flex;gap:10px;align-items:flex-start;font-size:19px;line-height:1.28}
.c-opts .ic{width:22px;height:22px;margin-top:3px}
.c-price{margin-top:auto;display:flex;justify-content:space-between;align-items:flex-end}
.price-label{font-size:17px}.price{font-size:86px;margin-top:10px}.price-note{font-size:18px;margin-top:10px}
.accent{height:4px;width:130px;background:linear-gradient(90deg,var(--acc),transparent)}
"""

# Раскладка полной комплектации по страницам: меряем реальную вёрстку после
# загрузки шрифтов и подбираем кегль, чтобы хвост не уходил на отдельную страницу
FLOW_JS = r"""
(async function () {
  const tpl = document.getElementById('full-page');
  if (!tpl) { document.body.dataset.ready = '1'; return; }
  try { await document.fonts.ready; } catch (e) {}
  const SIZES = [13.8, 13.2, 12.6, 12.0];
  function layout(fs) {
    document.querySelectorAll('section.p4').forEach((s) => s.remove());
    let pageNo = +tpl.dataset.start - 1, page, cols, ci;
    function newPage() {
      pageNo += 1;
      page = tpl.content.firstElementChild.cloneNode(true);
      page.style.setProperty('--opt-size', fs + 'px');
      page.querySelector('.pageno').textContent = String(pageNo).padStart(2, '0') + ' · Полная комплектация';
      page.querySelector('.section-title').textContent =
        'Полная комплектация' + (pageNo > +tpl.dataset.start ? ' (продолжение)' : '');
      tpl.parentNode.insertBefore(page, tpl);
      cols = page.querySelectorAll('.col'); ci = 0;
    }
    const over = (c) => c.scrollHeight > c.clientHeight + 1;
    function group(g) {
      const b = document.createElement('div'); b.className = 'group';
      b.innerHTML = '<h4>' + g.icon + '<span></span></h4><ul></ul>';
      b.querySelector('h4 span').textContent = g.title; cols[ci].appendChild(b); return b;
    }
    function fill(b, items) { for (const t of items) { const li = document.createElement('li'); li.textContent = t; b.querySelector('ul').appendChild(li); } }
    function next() { ci += 1; if (ci >= cols.length) newPage(); }
    newPage();
    for (const g of window.GROUPS) {
      if (g.items.length <= 8) {
        const p = group(g); fill(p, g.items); if (!over(cols[ci])) continue;
        p.remove(); next(); const w = group(g); fill(w, g.items); if (!over(cols[ci])) continue; w.remove();
      }
      let b = group(g);
      for (const t of g.items) {
        const li = document.createElement('li'); li.textContent = t; b.querySelector('ul').appendChild(li);
        if (!over(cols[ci])) continue;
        li.remove(); if (!b.querySelector('li')) b.remove(); next(); b = group(g); b.querySelector('ul').appendChild(li);
      }
    }
    const pages = document.querySelectorAll('section.p4');
    return { pages: pages.length, tail: pages[pages.length - 1].querySelectorAll('li').length };
  }
  let best = null;
  for (const fs of SIZES) {
    const r = layout(fs);
    if (!best || r.pages < best.pages) best = { fs: fs, pages: r.pages };
    if (r.pages === 1 || (r.pages === best.pages && r.tail >= 8)) break;
  }
  layout(best.fs);
  tpl.remove();
  document.body.dataset.ready = '1';
})();
"""


class Deck:
    """Всё, что нужно разметке: данные снимка, наценка, логотип, раскладка фото."""

    def __init__(self, snapshot: dict, markup_rub: int = 0, logo_src: str = "",
                 country: str = "", delivery: str = ""):
        d = snapshot["data"]
        self.d = d
        self.make, self.model, self.engine_tag = split_title(d.get("title") or "")
        self.price = int(snapshot["price_rub"]) + max(0, int(markup_rub or 0))
        self.logo_src = logo_src
        self.cover = cover_specs(d)
        self.details = detail_specs(d, country, delivery)
        self.options = [o for o in (snapshot.get("options") or []) if str(o).strip()]
        self.groups = categorize(self.options)
        self.country = country
        self.delivery = delivery
        trim = (d.get("attrs") or {}).get("trimLine", "")
        self.chips = [c for c in (self.engine_tag, d.get("drive") or "",
                                  trim if trim and trim != "-" and trim not in self.model else "",
                                  str(d.get("year") or "")) if c]

    # ── общие куски ──
    def logo(self) -> str:
        return f'<img class="logo" src="{E(self.logo_src)}">' if self.logo_src else ""

    def card(self, s) -> str:
        return (f'<div class="spec-card">{icon(s[0])}<div><div class="lbl">{E(s[1])}</div>'
                f'<div class="val">{E(s[2])}</div></div></div>')

    def chips_html(self) -> str:
        return "".join(f'<span class="chip">{E(c)}</span>' for c in self.chips)

    def price_block(self) -> str:
        return (f'<div class="price-label">Цена под ключ</div><div class="price">{rub(self.price)}</div>'
                f'<div class="price-note">в Москве · таможня и утильсбор включены</div>')

    def title_block(self) -> str:
        make = f'<div class="h-make">{E(self.make)}</div>' if self.make else ""
        chips = f'<div class="chips">{self.chips_html()}</div>' if self.chips else ""
        return f'{make}<div class="h-model">{E(self.model or self.make)}</div>{chips}'

    def _cat_of(self, opt: str) -> tuple[str, str]:
        for key, title, items in self.groups:
            if opt in items:
                return key, title
        return "plus", "Опция"

    # ── PDF ──
    def pdf_pages(self, layout: dict, src) -> str:
        pages = []
        n = 1
        cover_photo = _slot("cover", layout, src, "cover-photo")
        strip = ('<div class="strip">' + "".join(self.card(s) for s in self.cover) + "</div>"
                 if self.cover else "")
        pages.append(
            f'<section class="page bg cover{"" if cover_photo else " no-photo"}">{cover_photo}'
            f'<div class="topbar">{self.logo()}</div>'
            f'<div class="left">{self.title_block()}<div class="accent"></div>{self.price_block()}</div>'
            f'{strip}</section>')

        main = _slot("gallery_main", layout, src)
        smalls = [x for x in (_slot("gallery_a", layout, src), _slot("gallery_b", layout, src)) if x]
        if self.details or main:
            n += 1
            if main:
                row = (f'<div class="row{" one" if len(smalls) == 1 else ""}">{"".join(smalls)}</div>'
                       if smalls else "")
                gallery = f'<div class="gallery{"" if smalls else " single"}">{main}{row}</div>'
            else:
                gallery = ""
            details = (f'<div><div class="eyebrow">Подробно</div>'
                       f'<div class="section-title">Данные автомобиля</div>'
                       f'<div class="detail-grid">' + "".join(self.card(s) for s in self.details)
                       + "</div></div>") if self.details else ""
            pages.append(
                f'<section class="page bg p2{"" if gallery else " no-gallery"}">'
                f'<div class="topbar">{self.logo()}<div class="pageno">{n:02d} · Об автомобиле</div></div>'
                f'<div class="wrap">{gallery}{details}</div></section>')

        if self.options:
            n += 1
            photos = [x for x in (_slot("key_top", layout, src), _slot("key_bottom", layout, src)) if x]
            keys = key_options(self.options, 8 if photos else 9)
            keys_html = "".join(
                f'<div class="group">{icon(self._cat_of(o)[0])}<div><div class="c">{E(self._cat_of(o)[1])}</div>'
                f'<div class="t">{E(o)}</div></div></div>' for o in keys)
            photo_col = f'<div class="photos">{"".join(photos)}</div>' if photos else ""
            pages.append(
                f'<section class="page bg p3{"" if photos else " no-photos"}">'
                f'<div class="topbar">{self.logo()}<div class="pageno">{n:02d} · Главное в оснащении</div></div>'
                f'<div class="wrap"><div><div class="eyebrow">Ключевые опции</div>'
                f'<div class="section-title">Что делает этот<br>автомобиль особенным</div>'
                f'<div class="key-grid">{keys_html}</div></div>{photo_col}</div></section>')

            if len(self.options) <= len(keys):
                # весь список уже показан ключевыми опциями — второй раз не нужен
                pages.append("<script>document.body.dataset.ready='1'</script>")
                return "".join(pages)
            groups = json.dumps([{"icon": icon(k), "title": t, "items": i} for k, t, i in self.groups],
                                ensure_ascii=False).replace("</", "<\\/")
            pages.append(
                f'<template id="full-page" data-start="{n + 1}"><section class="page bg p4">'
                f'<div class="topbar">{self.logo()}<div class="pageno"></div></div>'
                f'<div class="eyebrow" style="margin-top:24px">Оснащение</div><div class="section-title"></div>'
                f'<div class="cols"><div class="col"></div><div class="col"></div><div class="col"></div></div>'
                f'</section></template><script>window.GROUPS={groups};</script><script>{FLOW_JS}</script>')
        else:
            pages.append("<script>document.body.dataset.ready='1'</script>")
        return "".join(pages)

    def pdf_html(self, layout: dict, src) -> str:
        return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{FONTS}'
                f'<style>{BASE_CSS}{PDF_CSS}</style></head><body>{self.pdf_pages(layout, src)}</body></html>')

    # ── Сторис ──
    def story_body(self, layout: dict, src) -> str:
        main = _slot("story_main", layout, src)
        smalls = [x for x in (_slot("story_a", layout, src), _slot("story_b", layout, src)) if x]
        collage = ""
        if main:
            row = (f'<div class="row{" one" if len(smalls) == 1 else ""}">{"".join(smalls)}</div>'
                   if smalls else "")
            collage = f'<div class="collage{"" if smalls else " single"}">{main}{row}</div>'

        extra = [s for s in self.details if s[1] in ("Салон", "Владельцев", "Состояние")]
        grid = (self.cover[:3] + extra)[:6]
        grid_html = ('<div class="c-grid">' + "".join(
            f'<div class="spec-card">{icon(s[0])}<div class="lbl">{E(s[1])}</div>'
            f'<div class="val">{E(s[2])}</div></div>' for s in grid) + "</div>") if grid else ""
        # фото меньше — места под опции больше
        n_opts = 10 if smalls else (14 if main else 20)
        opts = key_options(self.options, n_opts)
        opts_html = ('<div class="c-opts">' + "".join(
            f'<div>{icon("check")}<span>{E(o)}</span></div>' for o in opts) + "</div>") if opts else ""
        tag = " · ".join(x for x in (f"Из {self.country}" if self.country else "",
                                     self.delivery) if x)
        return (f'<div class="story bg"><div class="c-top">{self.logo()}'
                f'{f"<div class=tag>{E(tag)}</div>" if tag else ""}</div>{self.title_block()}'
                f'{collage}{grid_html}{opts_html}'
                f'<div class="c-price"><div>{self.price_block()}</div><div class="accent"></div></div></div>')

    def story_html(self, layout: dict, src) -> str:
        return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{FONTS}'
                f'<style>{BASE_CSS}{STORY_CSS}</style></head><body>{self.story_body(layout, src)}'
                f"<script>document.body.dataset.ready='1'</script></body></html>")


def file_name(snapshot: dict, ext: str) -> str:
    """«Volkswagen_Tiguan_R-Line_2023.pdf» — латиница, цифры, дефис."""
    d = snapshot["data"]
    make, model, engine = split_title(d.get("title") or "")
    # двигатель в имя не берём: «2.0 TDI» превращался в «2_0TDI»
    raw = "_".join(x for x in (make, model.replace("\u2011", "-"), str(d.get("year") or "")) if x)
    stem = re.sub(r"[^A-Za-z0-9\-]+", "_", raw).strip("_") or "presentation"
    return f"{stem}.{ext}"


# ── Фото ────────────────────────────────────────────────────────────────────

def crop_dealer_frame(img: Image.Image) -> Image.Image:
    """
    Дилерская белая рамка и полоса с названием салона снизу. Кадр — самый
    длинный отрезок строк, где почти нет белого.
    """
    g = img.convert("L")
    w, h = g.size
    small = g.resize((max(1, w // 4), max(1, h // 4)))
    sw, sh = small.size
    px = small.load()

    def row_white(y):
        return sum(1 for x in range(sw) if px[x, y] > 238) / sw

    best, start = (0, 0), None
    for y in range(sh + 1):
        photo = y < sh and row_white(y) < 0.6
        if photo and start is None:
            start = y
        elif not photo and start is not None:
            if y - start > best[1] - best[0]:
                best = (start, y)
            start = None
    y0, y1 = best
    if y1 - y0 < sh * 0.4:
        return img
    cols = [x for x in range(sw)
            if sum(1 for y in range(y0, y1) if px[x, y] > 238) / max(1, y1 - y0) < 0.6]
    x0, x1 = (cols[0], cols[-1] + 1) if cols else (0, sw)
    return img.crop((x0 * 4 + 4, y0 * 4 + 4, x1 * 4 - 4, y1 * 4 - 4))


async def cached_photo(url: str) -> Path | None:
    """Фото объявления на диске: скачано, без рамки дилера, не больше 2000px."""
    PHOTO_CACHE.mkdir(parents=True, exist_ok=True)
    path = PHOTO_CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".jpg")
    if path.exists():
        return path
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img = crop_dealer_frame(img)
        img.thumbnail((2000, 2000))
        tmp = path.with_suffix(".tmp")
        img.save(tmp, "JPEG", quality=88)
        tmp.replace(path)
        return path
    except Exception as exc:
        log.warning("Фото не скачалось (%s): %s", url[:80], exc)
        return None


def snapshot_photos(snapshot: dict) -> list[str]:
    """Фото КП первыми, дальше остальная галерея объявления, без повторов."""
    ph = snapshot.get("photos") or {}
    out: list[str] = []
    for u in (ph.get("chosen") or []) + (ph.get("all") or []):
        if u and u not in out:
            out.append(u)
    return out


# ── Chrome ──────────────────────────────────────────────────────────────────

def _chrome(*args: str) -> None:
    # Презентации собирают два процесса — бот и веб. Одно ядро на сервере
    # два Chrome сразу не тянет, поэтому очередь общая, через файл-замок
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with open(MEDIA_DIR / "render.lock", "w") as lock:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        except ImportError:          # Windows — локальная разработка
            pass
        _chrome_run(*args)


def _chrome_run(*args: str) -> None:
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
           "--disable-dev-shm-usage", "--allow-file-access-from-files",
           "--virtual-time-budget=20000", "--run-all-compositor-stages-before-draw", *args]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)


async def render(snapshot: dict, formats: list[str], layout: dict, markup_rub: int = 0,
                 logo_path: str = "", country: str = "", delivery: str = "") -> dict[str, Path]:
    """
    Собирает файлы. Возвращает {"pdf": путь, "story": путь} — только запрошенные.
    Файлы лежат во временной папке, вызывающий удаляет её после отправки.
    """
    urls = snapshot_photos(snapshot)
    needed = sorted({int(s["photo"]) for s in layout.values()
                     if s.get("photo") is not None and int(s["photo"]) < len(urls)})
    local: dict[int, Path] = {}
    for idx in needed:
        p = await cached_photo(urls[idx])
        if p:
            local[idx] = p
    # фото не скачалось — слот пропадает целиком, пустой рамки не будет
    layout = {k: v for k, v in layout.items() if v.get("photo") is not None and int(v["photo"]) in local}

    logo_src = Path(logo_path).resolve().as_uri() if logo_path and Path(logo_path).exists() else ""
    deck = Deck(snapshot, markup_rub, logo_src, country, delivery)

    def src(i: int) -> str:
        return local[i].resolve().as_uri()

    out_dir = Path(tempfile.mkdtemp(prefix="autokp_deck_"))
    result: dict[str, Path] = {}
    async with _render_lock:
        if "pdf" in formats:
            page = out_dir / "deck.html"
            page.write_text(deck.pdf_html(layout, src), encoding="utf-8")
            pdf = out_dir / file_name(snapshot, "pdf")
            await asyncio.to_thread(_chrome, "--no-pdf-header-footer", f"--print-to-pdf={pdf}", page.as_uri())
            result["pdf"] = pdf
        if "story" in formats:
            page = out_dir / "story.html"
            page.write_text(deck.story_html(layout, src), encoding="utf-8")
            png = out_dir / file_name(snapshot, "png")
            await asyncio.to_thread(_chrome, "--window-size=1080,1920", f"--screenshot={png}", page.as_uri())
            result["story"] = png
    return result
