"""
Расчёты по направлениям — единственный источник правды.
Используется и ботом, и веб-API, чтобы цифры всегда совпадали.

Направления:
  minsk  — лист B2B (ЕС/Минск):   таможня РБ в EUR + утиль вводятся руками, ЭПТС фикс.
  kult40 — лист B2B (ЕС/Культ40): брокер + эвакуатор + таможня ТКС + льготный утиль.
  msk    — лист B2B (ЕС-МСК):     то же, но без эвакуатора.
"""
from storage import get_float, get_tariffs

DIRECTIONS = ("minsk", "kult40", "msk")

DIRECTION_LABELS = {
    "minsk":  "🏙 ЕС/Минск",
    "kult40": "🏭 ЕС/Культ40",
    "msk":    "🌆 ЕС-МСК",
}

# Какие поля менеджер вводит руками для каждого направления
DIRECTION_FIELDS = {
    "minsk":  ["customs_eur", "util_rub"],
    "kult40": ["evacuator_rub", "customs_tks_rub"],
    "msk":    ["customs_tks_rub"],
}

FIELD_LABELS = {
    "customs_eur":     "Таможня РБ, €",
    "util_rub":        "Утиль, ₽",
    "customs_tks_rub": "Таможня ТКС, ₽",
    "evacuator_rub":   "Эвакуатор СПБ-МСК, ₽",
}

BUYBACK_PCTS = list(range(5, 16))   # 5–15 %


def fmt_eur(v: float) -> str:
    return f"{v:,.0f} €".replace(",", " ")


def fmt_rub(v: float) -> str:
    return f"{v:,.0f} ₽".replace(",", " ")


def tariffs_for(d: dict) -> dict:
    """
    Тарифы расчёта: снимок, сохранённый в записи, иначе текущие настройки.

    Снимок нужен, чтобы старые записи в истории не пересчитывались задним
    числом при изменении тарифов — иначе менеджер назовёт клиенту не ту цену,
    которую отправил.
    """
    current = get_tariffs()
    snap = d.get("tariffs")
    if isinstance(snap, dict) and snap:
        return {**current, **{k: v for k, v in snap.items() if v is not None}}
    return current


def util_fee(d: dict, tf: dict | None = None) -> float:
    """
    Утильсбор для Культ40 и МСК: сумма, названная менеджером, иначе льготный
    из настроек. Льготный положен не всякой машине, поэтому его нельзя считать
    молча — но старые записи в истории сохранялись без этого поля, и для них
    остаётся прежнее значение.
    """
    tf = tf or tariffs_for(d)
    val = d.get("util_rub") or 0
    return float(val) if val else float(tf["util_fixed_rub"])


def util_is_reduced(d: dict, tf: dict | None = None) -> bool:
    """Льготный ли утиль — от этого зависит подпись под ценой в КП."""
    tf = tf or tariffs_for(d)
    return util_fee(d, tf) == float(tf["util_fixed_rub"])


def default_logistics(direction: str, tf: dict | None = None) -> float:
    tf = tf or get_tariffs()
    return {
        "minsk":  tf["logistics_minsk"],
        "kult40": tf["logistics_kult40"],
        "msk":    tf["logistics_msk"],
    }.get(direction, tf["logistics_minsk"])


def vat_percent(vat: float) -> str:
    """Коэффициент 1.19 → «19%», 1.0 → «0%». В интерфейсе процент понятнее."""
    try:
        pct = (float(vat) - 1) * 100
    except (TypeError, ValueError):
        return "—"
    return f"{pct:g}%"


def vat_from_percent(pct: float) -> float:
    """Процент НДС → коэффициент: 23 → 1.23, 0 → 1.0 (без НДС)."""
    return 1 + max(0.0, float(pct)) / 100


def netto(d: dict) -> float:
    """Цена без НДС — от неё считается выкуп."""
    vat = d.get("vat", 1.19) or 1.19
    return (d.get("price_eur", 0) or 0) / vat


# ── Выкуп ─────────────────────────────────────────────────────────────────────

def buyback_options(h: float) -> list[dict]:
    """
    Проценты выкупа с суммами. below_min=True — сумма меньше минималки,
    такие варианты в интерфейсе заменяются одной кнопкой «минималка».
    """
    min_eur = get_float("buyback_min_eur", 2500)
    return [
        {"pct": pct, "eur": round(h * pct / 100, 2), "below_min": h * pct / 100 < min_eur}
        for pct in BUYBACK_PCTS
    ]


def apply_buyback(d: dict, mode: str, value: float) -> None:
    """
    Проставляет выкуп в данные расчёта.
      mode="pct"   — процент от НЕТТО
      mode="fixed" — фиксированная сумма в EUR (ручной ввод или минималка)
    """
    h = netto(d)
    if mode == "pct":
        k = h * value / 100
        d.update({
            "buyback_pct": value, "buyback_val": round(k, 2),
            "buyback_fixed": False, "buyback_is_min": False,
        })
    else:
        min_eur = get_float("buyback_min_eur", 2500)
        d.update({
            "buyback_val": round(value, 2),
            "buyback_pct": round(value / h * 100, 1) if h else 0,
            "buyback_fixed": True,
            "buyback_is_min": abs(value - min_eur) < 0.01,
        })


def buyback_label(d: dict) -> str:
    """«10%» для процента, «минималка» / «вручную» для фиксированной суммы."""
    if d.get("buyback_fixed"):
        return "минималка" if d.get("buyback_is_min") else "вручную"
    pct = d.get("buyback_pct", "—")
    if isinstance(pct, float):
        pct = int(pct) if pct.is_integer() else round(pct, 1)   # 12.0 → 12
    return f"{pct}%"


# ── Формулы направлений ───────────────────────────────────────────────────────

def calc_minsk(d: dict) -> dict:
    """Лист ЕС/Минск. Буквы в ключах — колонки таблицы."""
    tf  = tariffs_for(d)
    g   = d.get("price_eur", 0) or 0
    vat = d.get("vat", 1.19)
    h   = g / vat
    k   = d.get("buyback_val", 0) or 0
    j   = float(d.get("logistics") or tf["logistics_minsk"])
    l_  = h * tf["invoice_pct"] / 100 + tf["invoice_fix"]
    m   = h + j + k + l_ + tf["extra_fix"]
    # rate_n / rate_p / r_value — ключи старых записей в истории
    n   = d.get("rate_eur_usdt") or d.get("rate_n") or 1.1621
    p   = d.get("rate_usdt_rub") or d.get("rate_p") or 79.7
    o   = m * n
    q   = o * p
    r   = d.get("epts_rub") or d.get("r_value") or tf["epts_rub"]
    s   = d.get("customs_eur", 0) or 0
    t   = s * n * p
    u   = d.get("util_rub", 0) or 0
    v   = q + r + t + u
    return dict(g=g, vat=vat, h=h, k=k, j=j, l=l_, m=m, n=n, p=p,
                o=o, q=q, r=r, s=s, t=t, u=u, v=v)


def calc_v2(d: dict) -> dict:
    """Листы ЕС/Культ40 и ЕС-МСК."""
    tf        = tariffs_for(d)
    direction = d.get("direction", "kult40")
    f   = d.get("price_eur", 0) or 0          # F — БРУТТО
    vat = d.get("vat", 1.19)
    g   = f / vat                              # G — НЕТТО
    j   = float(d.get("logistics") or default_logistics(direction, tf))   # I — Логистика
    k   = d.get("buyback_val", g * 0.08)      # J — Выкуп
    l_  = g * tf["invoice_pct"] / 100 + tf["invoice_fix"]                 # K — Инвойс
    l_t = g + j + k + l_ + tf["extra_fix"]    # L — Итого EUR
    # rate_eur_usd / rate_usd_rub — ключи старых записей в истории
    m   = d.get("rate_eur_usdt") or d.get("rate_eur_usd") or 1.1621       # M — курс EUR/USDT
    n   = l_t * m                             # N — USDT
    o   = d.get("rate_usdt_rub") or d.get("rate_usd_rub") or 79.7         # O — курс USDT/₽
    p   = n * o                               # P — RUB
    q   = tf["broker_rub"]                    # Q — Брокер
    r   = d.get("evacuator_rub", 0) or 0      # R — Эвакуатор (Культ40)
    s   = d.get("customs_tks_rub", 0) or 0    # S (Культ40) / R (МСК) — Таможня ТКС
    t   = util_fee(d, tf)                     # T (Культ40) / S (МСК) — Утиль
    v   = p + q + r + s + t if direction == "kult40" else p + q + s + t
    return dict(f=f, vat=vat, g=g, j=j, k=k, l=l_, l_t=l_t,
                m=m, n=n, o=o, p=p, q=q, r=r, s=s, t=t, v=v)


def calc(d: dict) -> dict:
    """Расчёт по направлению из данных."""
    if d.get("direction") in ("kult40", "msk"):
        return calc_v2(d)
    return calc_minsk(d)


def total_rub(d: dict) -> float:
    """Итоговая цена под ключ."""
    return calc(d)["v"]


# ── Строки карточки (одинаковые в боте и в вебе) ─────────────────────────────

_SEP = {"label": "", "value": "", "role": "sep", "group": ""}


def _row(label: str, value: str, role: str = "detail", group: str = "") -> dict:
    """
    Строка расчёта. role определяет вес строки в интерфейсе:
      detail — подробность, в вебе прячется под «Подробно»
      key    — то, что менеджер выбрал сам (выкуп)
      sum    — промежуточный итог блока
      total  — финальная сумма, в вебе выносится отдельным блоком
    """
    return {"label": label, "value": value, "role": role, "group": group}


def card_rows(d: dict) -> list[dict]:
    """Расчёт построчно — одинаковый в боте и в вебе."""
    direction = d.get("direction", "minsk")
    pct = buyback_label(d)

    if direction in ("kult40", "msk"):
        c = calc_v2(d)
        rows = [
            _row("💶 БРУТТО (F)",        fmt_eur(c["f"]),   group="Стоимость в евро"),
            _row(f"📊 НДС {vat_percent(c['vat'])} → НЕТТО", fmt_eur(c["g"])),
            _row(f"🏦 Выкуп {pct} (J)",  fmt_eur(c["k"]),   role="key"),
            _row("📦 Логистика (I)",     fmt_eur(c["j"])),
            _row("📄 Инвойс (K)",        fmt_eur(c["l"])),
            _row("💰 Итого EUR (L)",     fmt_eur(c["l_t"]), role="sum"),
            _SEP,
            _row("📈 Курс EUR→USDT (M)", f"{c['m']}",       group="Перевод в рубли"),
            _row("💵 USDT (N)",          fmt_eur(c["n"])),
            _row("📈 Курс USDT→₽ (O)",   f"{c['o']}"),
            _row("💵 Итого ₽ (P)",       fmt_rub(c["p"]),   role="sum"),
            _SEP,
            _row("🏢 Брокер (Q)",        fmt_rub(c["q"]),   group="Расходы в России"),
        ]
        # Видно, льготный утиль или назначенный менеджером — цифры одинаковой
        # величины, а разница для клиента принципиальная
        util = fmt_rub(c["t"]) + ("" if util_is_reduced(d) else " (свой)")
        if direction == "kult40":
            rows += [
                _row("🚛 Эвакуатор СПБ-МСК (R)", fmt_rub(c["r"]) if c["r"] else "⏳ не указан"),
                _row("🛃 Таможня ТКС (S)",       fmt_rub(c["s"]) if c["s"] else "⏳ не указана"),
                _row("♻️ Утиль (T)",             util),
                _SEP,
                _row("✅ Итого (U)",             fmt_rub(c["v"]), role="total"),
            ]
        else:
            rows += [
                _row("🛃 Таможня ТКС (R)", fmt_rub(c["s"]) if c["s"] else "⏳ не указана"),
                _row("♻️ Утиль (S)",       util),
                _SEP,
                _row("✅ Итого (T)",       fmt_rub(c["v"]), role="total"),
            ]
        return rows

    c = calc_minsk(d)
    return [
        _row("💶 БРУТТО (G)",        fmt_eur(c["g"]),   group="Стоимость в евро"),
        _row(f"📊 НДС {vat_percent(c['vat'])} → НЕТТО", fmt_eur(c["h"])),
        _row(f"🏦 Выкуп {pct} (K)",  fmt_eur(c["k"]),   role="key"),
        _row("📦 Логистика (J)",     fmt_eur(c["j"])),
        _row("📄 Инвойс (L)",        fmt_eur(c["l"])),
        _row("💰 Итого EUR (M)",     fmt_eur(c["m"]),   role="sum"),
        _SEP,
        _row("📈 Курс EUR→USDT (N)", f"{c['n']}",       group="Перевод в рубли"),
        _row("💵 USDT (O)",          fmt_eur(c["o"])),
        _row("📈 Курс USDT→₽ (P)",   f"{c['p']}"),
        _row("💵 Итого ₽ (Q)",       fmt_rub(c["q"]),   role="sum"),
        _SEP,
        _row("🔧 ЭПТС/СБКТС (R)",    fmt_rub(c["r"]),   group="Расходы в России"),
        _row("🛃 Таможня EUR (S)",   fmt_eur(c["s"]) if c["s"] else "⏳ не указана"),
        _row("🛃 Таможня ₽ (T)",     fmt_rub(c["t"]) if c["s"] else "⏳"),
        _row("♻️ Утиль (U)",         fmt_rub(c["u"]) if c["u"] else "⏳ не указан"),
        _SEP,
        _row("✅ Итого с утилём (V)", fmt_rub(c["v"]), role="total"),
    ]
