"""
Дизайны презентации. «Оригинал» — синий 16:9 из presentation.py, остальные
четыре — 4:3 по референсам заказчика (Red Line, Graphite, Deep Navy, Soft Grey).

У четырёх референсов одна сетка и разные палитры, поэтому разметка здесь
одна, а тема — это цвета и шкала кеглей. Все координаты и кегли сняты
с PDF-референсов в пунктах (страница 1024×768 pt) — отсюда и единицы pt
в CSS: так страница совпадает с референсом один в один.

Модуль не импортирует presentation на уровне файла: presentation импортирует
его сам, и цикл импорта уронил бы оба.
"""
import json

# Палитры сняты из заливок и текста референсов
THEMES = {
    "redline": {
        "bg": "#101113", "ink": "#f7f7f5", "mute": "#999da3", "acc": "#b41f2c", "line": "#35383d",
        "matte": "#d8d8d8", "matte2": "#17191c",
        "p4bg": "#f7f7f5", "p4ink": "#141518", "p4mute": "#999da3", "p4rule": "#e6e7e9",
        "scale": "base", "light": False,
    },
    "graphite": {
        "bg": "#17191c", "ink": "#f4f4f2", "mute": "#9a9da1", "acc": "#6f757c", "line": "#34383d",
        "matte": "#d8d8d6", "matte2": "#1c1e20",
        "p4bg": "#ecece9", "p4ink": "#17191c", "p4mute": "#9a9da1", "p4rule": "#d2d3d1",
        "scale": "large", "light": False,
    },
    "navy": {
        "bg": "#101a27", "ink": "#f4f5f4", "mute": "#98a1ac", "acc": "#718096", "line": "#34383d",
        "matte": "#d8d8d6", "matte2": "#1c1e20",
        "p4bg": "#eef0f1", "p4ink": "#151a20", "p4mute": "#98a1ac", "p4rule": "#d2d3d1",
        "scale": "large", "light": False,
    },
    "softgrey": {
        "bg": "#e7e8e7", "ink": "#17191c", "mute": "#74777b", "acc": "#8b8177", "line": "#c8c9c8",
        "matte": "#d8d8d6", "matte2": "#1c1e20",
        "p4bg": "#f6f6f4", "p4ink": "#17191c", "p4mute": "#74777b", "p4rule": "#d2d3d1",
        "scale": "large", "light": True,
    },
}

# Кегли в pt. «base» — Audi R8, «large» — три варианта Larger Type
SCALES = {
    "large": dict(logo=14.5, make=9, model=48, sub=12.5, plabel=9.2, price=33, note=9.2, num=7.8,
                  slbl=8, sval=13.5, eyebrow=9, t2=27, dlbl=7.1, dval=11.1, t3=27.5, ktext=11.2,
                  p4head=9, p4num=7.1, p4item=9.5, p4price=19.5, p4note=8, acc_h=2, acc1=34, acc2=54, acc3=42),
    "base": dict(logo=13, make=8, model=46, sub=11, plabel=8, price=31, note=8, num=7,
                 slbl=7, sval=12, eyebrow=8, t2=25, dlbl=6.5, dval=10, t3=26, ktext=10,
                 p4head=8, p4num=6.5, p4item=8.6, p4price=18, p4note=7, acc_h=3, acc1=36, acc2=56, acc3=44),
}

DESIGNS = {
    "classic":  {"title": "Оригинал",  "description": "Синий, 16:9",              "pdf_size": [1600, 900]},
    "redline":  {"title": "Red Line",  "description": "Графит и красный акцент",  "pdf_size": [1365.33, 1024]},
    "graphite": {"title": "Graphite",  "description": "Тёмно-серый, крупный шрифт", "pdf_size": [1365.33, 1024]},
    "navy":     {"title": "Deep Navy", "description": "Глубокий синий",           "pdf_size": [1365.33, 1024]},
    "softgrey": {"title": "Soft Grey", "description": "Светлый серый",            "pdf_size": [1365.33, 1024]},
}


def design_id(layout) -> str:
    value = (layout or {}).get("_design", {})
    key = value.get("id") if isinstance(value, dict) else None
    return key if isinstance(key, str) and key in DESIGNS else "classic"


def is_editorial(theme: str) -> bool:
    return theme in THEMES


# ── Общие куски ─────────────────────────────────────────────────────────────

def _vars(t: dict, s: dict) -> str:
    colors = ";".join(f"--{k}:{v}" for k, v in t.items() if isinstance(v, str) and v.startswith("#"))
    sizes = ";".join(f"--{k}:{v}pt" for k, v in s.items())
    return f":root{{{colors};{sizes}}}"


def _fonts(deck) -> str:
    return ("<style>"
            f"@font-face{{font-family:'DejaVu Sans';src:url('{deck.font_src('dejavu')}') format('truetype');font-weight:400}}"
            f"@font-face{{font-family:'DejaVu Sans';src:url('{deck.font_src('dejavu-bold')}') format('truetype');font-weight:700}}"
            "</style>")


# Подгонка длинного названия модели под колонку: «R8» помещается,
# «Tiguan R-Line» при 48 pt — нет. Уменьшаем до читаемого минимума,
# дальше разрешаем перенос
FIT_JS = r"""
function fitText(root) {
  (root || document).querySelectorAll('[data-fit]').forEach((el) => {
    const min = parseFloat(el.dataset.fit);
    let size = parseFloat(getComputedStyle(el).fontSize);
    el.style.whiteSpace = 'nowrap';
    while (el.scrollWidth > el.clientWidth + 1 && size > min) {
      size -= 1; el.style.fontSize = size + 'px';
    }
    if (el.scrollWidth > el.clientWidth + 1) el.style.whiteSpace = 'normal';
  });
}
"""


def _logo(deck, dark_surface: bool) -> str:
    """
    Логотип контрагента. Светлый логотип на светлой странице пропал бы —
    под него кладём тёмную плашку (и наоборот не нужно: тёмное на тёмном
    встречается редко, а плашка на тёмном фоне смотрелась бы чужеродно).
    """
    if not deck.logo_src:
        return ""
    plate = " plate" if (deck.logo_light and not dark_surface) else ""
    return f'<div class="lg{plate}"><img src="{deck.logo_src}" alt=""></div>'


def _sub(deck) -> str:
    """«5.2 FSI quattro · 2020» — двигатель, фирменный привод и год."""
    engine = " ".join(x for x in (deck.engine_tag, deck.d.get("drive") or "") if x)
    return "  ·  ".join(x for x in (engine, str(deck.d.get("year") or "")) if x)


def _cover4(deck) -> list:
    """Нижняя строка обложки: четыре колонки, как в референсе. Привод — на второй странице."""
    return [s for s in deck.cover if s[1] != "Привод"][:4]


def _details(deck) -> list:
    """Данные автомобиля: семь строк по две колонки — больше сетка не вмещает."""
    tail_names = ("Страна", "Срок доставки")
    tail = [s for s in deck.details if s[1] in tail_names]
    items = [s for s in deck.details if s[1] not in tail_names]
    drive = next((s for s in deck.cover if s[1] == "Привод"), None)
    if drive:
        items.insert(min(2, len(items)), drive)
    # страну и срок доставки клиенту видно всегда — режем второстепенное
    return items[:14 - len(tail)] + tail


# ── PDF ─────────────────────────────────────────────────────────────────────

PDF_CSS = """
@page{size:1024pt 768pt;margin:0}
*{box-sizing:border-box;margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}
html,body{background:var(--bg)}
body{font-family:'DejaVu Sans',Arial,sans-serif;line-height:1.164}
.pg{width:1024pt;height:768pt;position:relative;overflow:hidden;background:var(--bg);color:var(--ink);page-break-after:always}
.pg:last-child{page-break-after:auto}
.abs{position:absolute}
.b{font-weight:700}
.up{text-transform:uppercase;letter-spacing:.06em}
.mute{color:var(--mute)}
.acc{position:absolute;background:var(--acc);height:var(--acc_h)}
.lg{position:absolute;left:48pt;top:34pt;height:26pt;max-width:200pt;display:flex;align-items:center}
.lg img{max-height:26pt;max-width:200pt;object-fit:contain;object-position:left center;display:block}
.lg.plate{background:#17191c;padding:4pt 8pt;border-radius:3pt;top:31pt;height:32pt}
.matte{position:absolute;background:var(--matte);overflow:hidden}
.matte.dark{background:var(--matte2)}
.matte .slot{width:100%;height:100%;background:transparent}
.slot{overflow:hidden;position:relative}
.slot img{width:100%;height:100%;object-fit:contain;display:block;user-select:none;-webkit-user-drag:none}
/* стр. 1 */
.c1-left{position:absolute;left:48pt;top:137pt;width:330pt}
.c1-make{font-size:var(--make);font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
.c1-model{font-size:var(--model);font-weight:700;margin-top:16.5pt;margin-left:-3pt;letter-spacing:-.01em;overflow:hidden}
.c1-sub{font-size:var(--sub);color:var(--mute);margin-top:8pt}
.c1-rule{height:var(--acc_h);width:var(--acc2);background:var(--acc);margin-top:28.5pt}
.c1-plabel{font-size:var(--plabel);font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.06em;margin-top:45pt}
.c1-price{font-size:var(--price);font-weight:700;margin-top:26pt;white-space:nowrap}
.c1-note{font-size:var(--note);color:var(--mute);margin-top:11.5pt}
.c1-specs{position:absolute;left:48pt;top:649pt;display:flex;gap:27pt}
.c1-spec{width:205pt;border-top:1pt solid var(--line);position:relative;height:70pt}
.num{font-size:var(--num);font-weight:700;color:var(--acc)}
.c1-spec .num{position:absolute;left:0;top:20pt}
.c1-spec .lbl{position:absolute;left:30pt;top:19pt;font-size:var(--slbl);font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
.c1-spec .val{position:absolute;left:30pt;top:45pt;font-size:var(--sval);font-weight:700;white-space:nowrap}
/* стр. 2 */
.eyebrow{font-size:var(--eyebrow);font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
.t2{font-size:var(--t2);font-weight:700}
.d-item{position:absolute;width:190pt}
.d-item .lbl{font-size:var(--dlbl);font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.06em}
.d-item .val{position:absolute;top:22pt;left:0;width:190pt;font-size:var(--dval);font-weight:700;line-height:1.26;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.d-rule{position:absolute;width:190pt;height:0;border-top:1pt solid var(--line)}
/* стр. 3 */
.t3{font-size:var(--t3);font-weight:700;line-height:1.38}
.k-item{position:absolute;width:250pt}
.k-item .num{position:absolute;left:0;top:3pt}
.k-item .txt{position:absolute;left:32pt;top:0;width:218pt;font-size:var(--ktext);font-weight:700;line-height:1.385;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.k-rule{position:absolute;width:250pt;height:0;border-top:1pt solid var(--line)}
/* стр. 4 — полная комплектация на светлом фоне */
.p4{background:var(--p4bg);color:var(--p4ink)}
.p4-head{position:absolute;right:48pt;top:45pt;font-size:var(--p4head);font-weight:700;color:var(--p4mute)}
.p4-top{position:absolute;left:48pt;right:48pt;top:88pt;border-top:1pt solid var(--p4ink)}
.p4-bot{position:absolute;left:48pt;right:48pt;top:696pt;border-top:1pt solid var(--p4rule)}
.p4-cols{position:absolute;left:48pt;top:123pt;height:552pt;display:flex;gap:28pt}
.p4-col{width:282pt;height:100%;overflow:hidden}
.p4-col li{list-style:none;display:flex;gap:0;margin-bottom:11.5pt;font-size:var(--p4item);line-height:1.33}
.p4-col li .num{width:28pt;flex:none;font-size:var(--p4num);padding-top:2pt}
.p4-price{position:absolute;left:48pt;top:716pt;display:flex;align-items:baseline;gap:30pt}
.p4-price .v{font-size:var(--p4price);font-weight:700;white-space:nowrap}
.p4-price .n{font-size:var(--p4note);color:var(--p4mute)}
"""


def _slot_in(name: str, layout: dict, src, box: tuple, dark: bool = False) -> str:
    """Подложка с фото. Нет фото для рамки — нет и подложки."""
    from presentation import _slot
    inner = _slot(name, layout, src)
    if not inner:
        return ""
    x, y, w, h = box
    return (f'<div class="matte{" dark" if dark else ""}" '
            f'style="left:{x}pt;top:{y}pt;width:{w}pt;height:{h}pt">{inner}</div>')


def pdf_html(deck, layout: dict, src, theme: str, only_cover: bool = False) -> str:
    from presentation import E, key_options, rub
    t, s = THEMES[theme], SCALES[THEMES[theme]["scale"]]
    dark = not t["light"]
    pages = []

    # ── 1. Обложка ──
    specs = "".join(
        f'<div class="c1-spec"><span class="num">{i + 1:02d}</span>'
        f'<span class="lbl">{E(lbl)}</span><span class="val">{E(val)}</span></div>'
        for i, (_, lbl, val) in enumerate(_cover4(deck)))
    sub = _sub(deck)
    pages.append(
        f'<section class="pg">{_logo(deck, dark)}'
        f'<div class="acc" style="left:48pt;top:70pt;width:var(--acc1)"></div>'
        f'<div class="c1-left">'
        f'{f"<div class=c1-make>{E(deck.make)}</div>" if deck.make else ""}'
        f'<div class="c1-model" data-fit="30">{E(deck.model or deck.make)}</div>'
        f'{f"<div class=c1-sub>{E(sub)}</div>" if sub else ""}'
        f'<div class="c1-rule"></div>'
        f'<div class="c1-plabel">Цена под ключ</div>'
        f'<div class="c1-price">{rub(deck.price)}</div>'
        f'<div class="c1-note">в Москве · таможня и утильсбор включены</div></div>'
        f'{_slot_in("cover", layout, src, (396, 58, 580, 500))}'
        f'{f"<div class=c1-specs>{specs}</div>" if specs else ""}</section>')

    if not only_cover:
        # ── 2. Данные автомобиля ──
        details = _details(deck)
        photos = (_slot_in("gallery_main", layout, src, (48, 94, 450, 398))
                  + _slot_in("gallery_a", layout, src, (48, 514, 218, 180), dark=True)
                  + _slot_in("gallery_b", layout, src, (280, 514, 218, 180), dark=True))
        if details or photos:
            rows = []
            for i, (_, lbl, val) in enumerate(details):
                x, r = 548 + (i % 2) * 214, i // 2
                rows.append(
                    f'<div class="d-item" style="left:{x}pt;top:{208 + 69 * r}pt">'
                    f'<div class="lbl">{E(lbl)}</div><div class="val">{E(val)}</div></div>'
                    f'<div class="d-rule" style="left:{x}pt;top:{258 + 69 * r}pt"></div>')
            eyebrow = " ".join(x for x in (deck.make, deck.model.replace("‑", "-")) if x).upper()
            year = str(deck.d.get("year") or "")
            pages.append(
                f'<section class="pg">{_logo(deck, dark)}'
                f'<div class="acc" style="left:48pt;top:70pt;width:var(--acc1)"></div>{photos}'
                f'<div class="abs eyebrow" style="left:548pt;top:84pt">'
                f'{E(eyebrow)}{("  /  " + E(year)) if year else ""}</div>'
                f'<div class="abs t2" style="left:548pt;top:119pt">Данные автомобиля</div>'
                f'<div class="acc" style="left:548pt;top:166pt;width:var(--acc3)"></div>'
                f'{"".join(rows)}</section>')

        # ── 3. Ключевые опции ──
        if deck.options:
            keys = key_options(deck.options, 8)
            items = []
            for i, opt in enumerate(keys):
                x, r = 48 + (i % 2) * 284, i // 2
                items.append(
                    f'<div class="k-item" style="left:{x}pt;top:{293 + 92 * r}pt">'
                    f'<span class="num">{i + 1:02d}</span><span class="txt">{E(opt)}</span></div>'
                    f'<div class="k-rule" style="left:{x}pt;top:{360 + 92 * r}pt"></div>')
            photos3 = (_slot_in("key_top", layout, src, (642, 93, 334, 286))
                       + _slot_in("key_bottom", layout, src, (642, 408, 334, 286)))
            pages.append(
                f'<section class="pg">{_logo(deck, dark)}'
                f'<div class="acc" style="left:48pt;top:70pt;width:var(--acc1)"></div>'
                f'<div class="abs eyebrow" style="left:48pt;top:115pt">Ключевые опции</div>'
                f'<div class="abs t3" style="left:48pt;top:138pt">Что делает этот<br>автомобиль особенным</div>'
                f'<div class="acc" style="left:48pt;top:233pt;width:var(--acc3)"></div>'
                f'{"".join(items)}{photos3}</section>')

            # ── 4. Полная комплектация — раскладывается скриптом по колонкам ──
            if len(deck.options) > len(keys):
                head = " ".join(x for x in (deck.make, deck.model.replace("‑", "-")) if x).upper()
                head = "  ·  ".join(x for x in (head, _sub(deck)) if x)
                pages.append(
                    f'<template id="ed-full"><section class="pg p4">{_logo(deck, False)}'
                    f'<div class="acc" style="left:48pt;top:70pt;width:var(--acc1)"></div>'
                    f'<div class="p4-head">{E(head)}</div><div class="p4-top"></div>'
                    f'<div class="p4-cols"><ul class="p4-col"></ul><ul class="p4-col"></ul><ul class="p4-col"></ul></div>'
                    f'<div class="p4-bot"></div></section></template>'
                    f'<div id="ed-price" hidden><div class="p4-price"><span class="v">{rub(deck.price)}</span>'
                    f'<span class="n">под ключ в Москве · таможня и утильсбор включены</span></div></div>'
                    f'<script>window.ED_OPTIONS={json.dumps(deck.options, ensure_ascii=False).replace("</", "<\\/")};</script>')

    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{_fonts(deck)}'
            f'<style>{_vars(t, s)}{PDF_CSS}</style></head><body class="ed">{"".join(pages)}'
            f'<script>{FIT_JS}{FLOW_JS}</script></body></html>')


# Полная комплектация: сквозная нумерация, три колонки, продолжение на новой
# странице. Длинный список сначала пробуем уместить мельче, как в «Оригинале»
FLOW_JS = r"""
(async function () {
  try { await document.fonts.ready; } catch (e) {}
  fitText(document);
  const tpl = document.getElementById('ed-full');
  if (!tpl) { document.body.dataset.ready = '1'; return; }
  const opts = window.ED_OPTIONS || [];
  function layout(k) {
    document.querySelectorAll('section.p4').forEach((s) => s.remove());
    let page, cols, ci, n = 0;
    function newPage() {
      page = tpl.content.firstElementChild.cloneNode(true);
      page.style.setProperty('--k', k);
      tpl.parentNode.insertBefore(page, tpl);
      cols = page.querySelectorAll('.p4-col'); ci = 0;
    }
    newPage();
    for (const text of opts) {
      n += 1;
      const li = document.createElement('li');
      li.innerHTML = '<span class="num"></span><span class="t"></span>';
      li.querySelector('.num').textContent = String(n).padStart(2, '0');
      li.querySelector('.t').textContent = text;
      li.style.fontSize = 'calc(var(--p4item) * ' + k + ')';
      cols[ci].appendChild(li);
      if (cols[ci].scrollHeight > cols[ci].clientHeight + 1) {
        li.remove(); ci += 1;
        if (ci >= cols.length) newPage();
        cols[ci].appendChild(li);
      }
    }
    return document.querySelectorAll('section.p4').length;
  }
  let pages = 0, k = 1;
  for (k of [1, 0.94, 0.88]) { pages = layout(k); if (pages === 1) break; }
  if (pages > 1) { k = 1; layout(1); }
  // Уместилось на страницу — выравниваем колонки, как в референсе: иначе
  // первые две забиты под завязку, а третья пустая
  if (pages === 1) {
    const cols = document.querySelectorAll('section.p4 .p4-col');
    const items = [...document.querySelectorAll('section.p4 .p4-col li')];
    const per = Math.ceil(items.length / cols.length);
    items.forEach((li, i) => cols[Math.min(cols.length - 1, Math.floor(i / per))].appendChild(li));
    if ([...cols].some((c) => c.scrollHeight > c.clientHeight + 1)) layout(k);
  }
  const all = document.querySelectorAll('section.p4');
  all[all.length - 1].appendChild(document.querySelector('#ed-price .p4-price'));
  tpl.remove();
  document.body.dataset.ready = '1';
})();
"""


# ── Сторис 1080×1920 ─────────────────────────────────────────────────────────

STORY_CSS = """
@page{size:1080px 1920px;margin:0}
*{box-sizing:border-box;margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}
html,body{width:1080px;height:1920px;overflow:hidden;background:var(--bg)}
body{font-family:'DejaVu Sans',Arial,sans-serif;line-height:1.164}
.st{width:1080px;height:1920px;position:relative;overflow:hidden;background:var(--bg);color:var(--ink);
  padding:78px 80px 70px;display:flex;flex-direction:column}
/* блоки не сжимаются: иначе при нехватке места flex схлопывал название модели в ноль */
.st>*{flex-shrink:0}
.st-top{display:flex;justify-content:space-between;align-items:flex-start;min-height:48px}
.st .lg{position:static;height:48px;max-width:360px;display:flex;align-items:center}
.st .lg img{max-height:48px;max-width:360px;object-fit:contain;object-position:left center;display:block}
.st .lg.plate{background:#17191c;padding:8px 14px;border-radius:6px;height:64px}
.st-tag{font-size:17px;font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.08em;margin-left:auto;padding-top:14px}
.st-acc{height:calc(var(--acc_h) * 2);width:calc(var(--acc1) * 2);background:var(--acc);margin-top:18px}
.st-make{font-size:24px;font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.08em;margin-top:52px}
.st-model{font-size:124px;font-weight:700;margin-top:14px;margin-left:-6px;letter-spacing:-.01em;overflow:hidden}
.st-sub{font-size:30px;color:var(--mute);margin-top:14px}
.st-photos{margin-top:36px;display:grid;grid-template-rows:520px 240px;gap:10px}
.st-photos.single{grid-template-rows:620px}
.st-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.st-row.one{grid-template-columns:1fr}
.st-matte{background:var(--matte);overflow:hidden}
.st-matte.dark{background:var(--matte2)}
.st-matte .slot{width:100%;height:100%}
.slot{overflow:hidden;position:relative}
.slot img{width:100%;height:100%;object-fit:contain;display:block;user-select:none;-webkit-user-drag:none}
.st-specs{display:flex;gap:22px;margin-top:40px}
.st-spec{flex:1;border-top:2px solid var(--line);padding-top:20px;position:relative}
.st-spec .num{font-size:15px;font-weight:700;color:var(--acc)}
.st-spec .lbl{font-size:15px;font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.06em;margin-left:12px}
.st-spec .val{font-size:28px;font-weight:700;margin-top:14px;white-space:nowrap}
.st-keys{display:grid;grid-template-columns:1fr 1fr;gap:0 40px;margin-top:34px}
.st-key{display:flex;gap:18px;padding:16px 0;border-bottom:2px solid var(--line);font-size:23px;font-weight:700;line-height:1.3}
.st-key .num{font-size:15px;font-weight:700;color:var(--acc);padding-top:6px;flex:none}
.st-price{margin-top:auto}
.st-plabel{font-size:18px;font-weight:700;color:var(--mute);text-transform:uppercase;letter-spacing:.08em}
.st-pval{font-size:92px;font-weight:700;margin-top:12px;white-space:nowrap}
.st-note{font-size:20px;color:var(--mute);margin-top:12px}
.st-rule{height:calc(var(--acc_h) * 2);width:calc(var(--acc2) * 2);background:var(--acc);margin-top:30px}
"""


def story_html(deck, layout: dict, src, theme: str) -> str:
    from presentation import E, _slot, key_options, rub
    t, s = THEMES[theme], SCALES[THEMES[theme]["scale"]]
    dark = not t["light"]

    main = _slot("story_main", layout, src)
    smalls = [x for x in (_slot("story_a", layout, src), _slot("story_b", layout, src)) if x]
    photos = ""
    if main:
        row = ("".join(f'<div class="st-matte dark">{x}</div>' for x in smalls))
        row = f'<div class="st-row{" one" if len(smalls) == 1 else ""}">{row}</div>' if smalls else ""
        photos = (f'<div class="st-photos{"" if smalls else " single"}">'
                  f'<div class="st-matte">{main}</div>{row}</div>')

    specs = "".join(
        f'<div class="st-spec"><span class="num">{i + 1:02d}</span><span class="lbl">{E(lbl)}</span>'
        f'<div class="val">{E(val)}</div></div>' for i, (_, lbl, val) in enumerate(_cover4(deck)[:3]))
    n_keys = 4 if smalls else (6 if main else 12)
    keys = "".join(
        f'<div class="st-key"><span class="num">{i + 1:02d}</span><span>{E(o)}</span></div>'
        for i, o in enumerate(key_options(deck.options, n_keys)))
    tag = " · ".join(x for x in (f"Из {deck.country}" if deck.country else "", deck.delivery) if x)
    sub = _sub(deck)

    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{_fonts(deck)}'
            f'<style>{_vars(t, s)}{STORY_CSS}</style></head><body class="ed"><div class="st">'
            f'<div class="st-top">{_logo(deck, dark)}{f"<div class=st-tag>{E(tag)}</div>" if tag else ""}</div>'
            f'<div class="st-acc"></div>'
            f'{f"<div class=st-make>{E(deck.make)}</div>" if deck.make else ""}'
            f'<div class="st-model" data-fit="64">{E(deck.model or deck.make)}</div>'
            f'{f"<div class=st-sub>{E(sub)}</div>" if sub else ""}'
            f'{photos}'
            f'{f"<div class=st-specs>{specs}</div>" if specs else ""}'
            f'{f"<div class=st-keys>{keys}</div>" if keys else ""}'
            f'<div class="st-price"><div class="st-rule"></div><div class="st-plabel" style="margin-top:26px">Цена под ключ</div>'
            f'<div class="st-pval">{rub(deck.price)}</div>'
            f'<div class="st-note">в Москве · таможня и утильсбор включены</div></div>'
            f'</div><script>{FIT_JS}(async function(){{try{{await document.fonts.ready}}catch(e){{}}'
            f"fitText(document);document.body.dataset.ready='1'}})();</script></body></html>")
