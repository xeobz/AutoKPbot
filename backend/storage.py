"""
SQLite storage for bot settings and pending (incomplete) car requests.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from kp import norm_brand

DB_PATH = Path(__file__).parent / "bot_data.db"

SCHEMA_VERSION = "2"

_DEFAULTS = {
    # ── Курсы — единые для всех направлений ──────────────────────────────────
    "rate_eur_usdt":    "1.1621",  # EUR → USDT (Минск: N, Культ40/МСК: M)
    "rate_usdt_rub":    "79.7",    # USDT → ₽   (Минск: P, Культ40/МСК: O)
    "rates_date":       "",        # дата, на которую заданы курсы (дд.мм.гггг)
    "rates_set_by":     "",        # кто задал курс дня
    # ── Тарифы ────────────────────────────────────────────────────────────────
    "logistics_minsk":  "5900",    # € логистика ЕС/Минск
    "logistics_kult40": "4900",    # € логистика ЕС/Культ40
    "logistics_msk":    "2750",    # € СВХ МСК
    "broker_rub":       "120000",  # ₽ брокер (Культ40 и СВХ МСК)
    "util_fixed_rub":   "5200",    # ₽ льготный утиль (Культ40 и СВХ МСК)
    "epts_rub":         "45000",   # ₽ ЭПТС/СБКТС (только Минск)
    "invoice_pct":      "1.3",     # % инвойса от НЕТТО
    "invoice_fix":      "100",     # € фикс. часть инвойса
    "extra_fix":        "350",     # € прочие расходы
    "buyback_min_eur":  "2500",    # € минимальный выкуп
    # ── фото ──────────────────────────────────────────────────────────────────
    "img_offset":       "0",       # пропустить первых N фото
    "img_count":        "6",       # сколько фото в КП
    "img_step":         "2",       # шаг выборки: 2 = каждое второе
}

# Старый ключ → новый (переезд на единые курсы и явные названия тарифов)
_RENAMED = {
    "rate_n":  "rate_eur_usdt",
    "rate_p":  "rate_usdt_rub",
    "r_value": "epts_rub",
}

# Тарифы, которые обновились у клиента — применяются один раз при переходе на v2
_V2_TARIFFS = {
    "logistics_minsk":  "5900",
    "logistics_kult40": "4900",
    "logistics_msk":    "2750",
    "broker_rub":       "120000",
}

# Ключи, которые больше не используются (единый курс вместо двух пар)
_OBSOLETE = ("rate_eur_usd", "rate_usd_rub")

# Что можно править из интерфейса: ключ → (название, единица, раздел).
# Используется и меню бота, и настройками в мини-аппе.
EDITABLE_SETTINGS = {
    "rate_eur_usdt":    ("Курс EUR→USDT",              "",   "rates"),
    "rate_usdt_rub":    ("Курс USDT→₽",                "",   "rates"),
    "logistics_minsk":  ("Логистика ЕС/Минск",         "€",  "tariffs"),
    "logistics_kult40": ("Логистика ЕС/Культ40",       "€",  "tariffs"),
    "logistics_msk":    ("СВХ МСК",                    "€",  "tariffs"),
    "broker_rub":       ("Брокер (Культ40 и СВХ МСК)", "₽",  "tariffs"),
    "util_fixed_rub":   ("Утиль льготный (Культ40/МСК)", "₽", "tariffs"),
    "epts_rub":         ("ЭПТС/СБКТС (только Минск)",  "₽",  "tariffs"),
    "invoice_pct":      ("Инвойс, процент",            "%",  "tariffs"),
    "invoice_fix":      ("Инвойс, фикс. часть",        "€",  "tariffs"),
    "extra_fix":        ("Прочие расходы",             "€",  "tariffs"),
    "buyback_min_eur":  ("Минимальный выкуп",          "€",  "tariffs"),
    "img_count":        ("Сколько фото в КП",          "шт", "photo"),
    "img_step":         ("Шаг выборки фото",           "",   "photo"),
    "img_offset":       ("Пропустить первых фото",     "шт", "photo"),
}

SECTION_TITLES = {
    "rates":   "Курс дня",
    "tariffs": "Тарифы",
    "photo":   "Фото",
}


@contextmanager
def _conn():
    """
    Соединение с базой. Коммитит при выходе и обязательно закрывает —
    в базу теперь ходят два процесса (бот и веб), нельзя копить хендлы.
    """
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        # WAL — чтобы бот и веб не блокировали друг друга на записи
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                manager_name TEXT,
                car_label    TEXT,
                data_json    TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL UNIQUE,
                name        TEXT    NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                car_num     INTEGER,
                sheet_row   INTEGER,
                car_name    TEXT,
                counterparty TEXT,
                url         TEXT,
                data_json   TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS drafts (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                chat_id    INTEGER NOT NULL,
                data_json  TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'open'
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS brand_emoji (
                brand           TEXT PRIMARY KEY,   -- нижним регистром: "skoda"
                custom_emoji_id TEXT NOT NULL,
                emoji           TEXT NOT NULL DEFAULT '',
                added_by        TEXT NOT NULL DEFAULT ''
            )
        """)
        # Seed defaults (ignore if already set)
        for k, v in _DEFAULTS.items():
            con.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
    _migrate()


def _migrate() -> None:
    """
    Переезд старой базы на новую схему настроек:
      • два курса (Минск / Культ40) → один общий;
      • r_value → epts_rub;
      • новые тарифы клиента (логистика, брокер) применяются принудительно.
    Выполняется один раз — отмечается ключом schema_version.
    """
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key='schema_version'"
        ).fetchone()
        if row and row["value"] == SCHEMA_VERSION:
            return

        existing = {
            r["key"]: r["value"]
            for r in con.execute("SELECT key, value FROM settings").fetchall()
        }

        # 1. Переносим значения переименованных ключей
        for old, new in _RENAMED.items():
            if old in existing and existing.get(new, "") in ("", _DEFAULTS.get(new)):
                con.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (new, existing[old]),
                )
            con.execute("DELETE FROM settings WHERE key=?", (old,))

        # 2. Новые тарифы — перетираем старые значения
        for k, v in _V2_TARIFFS.items():
            con.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )

        # 3. Выбрасываем ключи, которых больше нет
        for k in _OBSOLETE:
            con.execute("DELETE FROM settings WHERE key=?", (k,))

        con.execute(
            "INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str) -> str:
    with _conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else _DEFAULTS.get(key, "0")


def get_optional(key: str) -> str | None:
    """
    Значение настройки или None, если её не задавали.

    get_setting для незнакомого ключа отдаёт строку «0» (удобно для чисел),
    но для текстовых настроек вроде эмодзи это мусор, который подставился бы
    в КП вместо символа.
    """
    value = get_setting(key)
    return value if value and value != "0" else None


def set_setting(key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_all_settings() -> dict:
    with _conn() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(key))
    except (TypeError, ValueError):
        try:
            return float(_DEFAULTS.get(key, default))
        except (TypeError, ValueError):
            return default


# ── Курс дня ──────────────────────────────────────────────────────────────────

def get_rates() -> dict:
    """Единый курс для всех направлений + информация, на какой день он задан."""
    s = get_all_settings()

    def _f(key: str) -> float:
        try:
            return float(s.get(key, _DEFAULTS[key]))
        except (TypeError, ValueError):
            return float(_DEFAULTS[key])

    return {
        "rate_eur_usdt": _f("rate_eur_usdt"),
        "rate_usdt_rub": _f("rate_usdt_rub"),
        "rates_date":    s.get("rates_date", ""),
        "rates_set_by":  s.get("rates_set_by", ""),
    }


def set_rates(eur_usdt: float, usdt_rub: float, date_str: str, who: str) -> None:
    set_setting("rate_eur_usdt", str(eur_usdt))
    set_setting("rate_usdt_rub", str(usdt_rub))
    set_setting("rates_date", date_str)
    set_setting("rates_set_by", who)


def rates_are_fresh(today: str) -> bool:
    """True, если курс уже задан на сегодняшнюю дату."""
    return get_setting("rates_date") == today


# ── Тарифы ────────────────────────────────────────────────────────────────────

def get_tariffs() -> dict:
    return {
        "logistics_minsk":  get_float("logistics_minsk"),
        "logistics_kult40": get_float("logistics_kult40"),
        "logistics_msk":    get_float("logistics_msk"),
        "broker_rub":       get_float("broker_rub"),
        "util_fixed_rub":   get_float("util_fixed_rub"),
        "epts_rub":         get_float("epts_rub"),
        "invoice_pct":      get_float("invoice_pct"),
        "invoice_fix":      get_float("invoice_fix"),
        "extra_fix":        get_float("extra_fix"),
        "buyback_min_eur":  get_float("buyback_min_eur"),
    }


# ── Черновики расчётов (мини-апп) ────────────────────────────────────────────

def save_draft(draft_id: str, user_id: int, chat_id: int, data: dict) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO drafts (id, user_id, chat_id, data_json, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'open') ON CONFLICT(id) DO UPDATE SET "
            "data_json=excluded.data_json, chat_id=excluded.chat_id",
            (draft_id, user_id, chat_id, json.dumps(data, ensure_ascii=False),
             datetime.now().strftime("%d.%m.%Y %H:%M")),
        )


def get_draft(draft_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["data"] = json.loads(rec.pop("data_json"))
    return rec


def close_draft(draft_id: str) -> None:
    with _conn() as con:
        con.execute("UPDATE drafts SET status='done' WHERE id=?", (draft_id,))


def cleanup_drafts(keep_last: int = 500) -> None:
    """Удаляет завершённые черновики, кроме последних keep_last. Зовётся при старте."""
    with _conn() as con:
        con.execute(
            "DELETE FROM drafts WHERE status='done' AND id IN "
            "(SELECT id FROM drafts ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
            (keep_last,),
        )


# ── Эмодзи марок (премиум custom emoji) ──────────────────────────────────────

def set_brand_emoji(brand: str, custom_emoji_id: str, emoji: str, added_by: str = "") -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO brand_emoji (brand, custom_emoji_id, emoji, added_by) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(brand) DO UPDATE SET "
            "custom_emoji_id=excluded.custom_emoji_id, emoji=excluded.emoji, "
            "added_by=excluded.added_by",
            (norm_brand(brand), custom_emoji_id, emoji, added_by),
        )


def get_brand_emoji(brand: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM brand_emoji WHERE brand=?", (norm_brand(brand),)
        ).fetchone()
    return dict(row) if row else None


def get_all_brand_emoji() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT * FROM brand_emoji ORDER BY brand").fetchall()
    return [dict(r) for r in rows]


def remove_brand_emoji(brand: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM brand_emoji WHERE brand=?", (norm_brand(brand),))


# ── Pending requests ──────────────────────────────────────────────────────────

def save_pending(user_id: int, chat_id: int, manager_name: str, car_label: str, data: dict) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO pending (user_id, chat_id, manager_name, car_label, data_json, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (user_id, chat_id, manager_name, car_label, json.dumps(data, ensure_ascii=False),
             datetime.now().strftime("%d.%m.%Y %H:%M")),
        )
        return cur.lastrowid


def get_pending_for_user(user_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM pending WHERE user_id=? AND status='pending' ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_by_id(pending_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM pending WHERE id=?", (pending_id,)).fetchone()
    return dict(row) if row else None


def complete_pending(pending_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE pending SET status='done' WHERE id=?", (pending_id,))


# ── Admins ───────────────────────────────────────────────────────────────────

def get_all_admins() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT telegram_id, name FROM admins ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_admin_ids() -> set[int]:
    with _conn() as con:
        rows = con.execute("SELECT telegram_id FROM admins").fetchall()
    return {r["telegram_id"] for r in rows}


def add_admin(telegram_id: int, name: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO admins (telegram_id, name) VALUES (?, ?)",
            (telegram_id, name),
        )


def remove_admin(telegram_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM admins WHERE telegram_id=?", (telegram_id,))


# ── History ───────────────────────────────────────────────────────────────────

def save_history(
    user_id: int, chat_id: int, car_num: int, sheet_row: int,
    car_name: str, counterparty: str, url: str, data: dict,
) -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO history
               (user_id, chat_id, car_num, sheet_row, car_name, counterparty, url, data_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, chat_id, car_num, sheet_row, car_name, counterparty, url,
             json.dumps(data, ensure_ascii=False),
             datetime.now().strftime("%d.%m.%Y %H:%M")),
        )
        return cur.lastrowid


def get_history_for_user(user_id: int, limit: int = 20) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT id, car_num, car_name, counterparty, created_at
               FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_history_by_id(record_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM history WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def update_history_data(record_id: int, data: dict) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE history SET data_json=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), record_id),
        )
