"""
SQLite storage for bot settings and pending (incomplete) car requests.
"""
import json
import os
import secrets
import re
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
    "customs_kf_minsk": "1.01",    # надбавка к таможне РБ (столбец S)
    "invoice_pct":      "1.3",     # % инвойса от НЕТТО
    "invoice_fix":      "100",     # € фикс. часть инвойса
    "extra_fix":        "350",     # € прочие расходы
    "buyback_min_eur":  "2500",    # € минимальный выкуп
    # ── комплектация ──────────────────────────────────────────────────────────
    # Строки, которые не нужны клиенту в КП: они есть у любой машины и только
    # занимают место. Список правится в настройках, разделитель — запятая.
    # Страна — в форме после «из», срок доставки — как его пишут клиенту
    "kp_country":  "Германии",
    # Срок доставки: у ЕС-МСК он свой, там машина едет напрямую на СВХ
    "kp_delivery":     "до 45 дней",
    "kp_delivery_msk": "до 30 дней",
    "kp_exclude": "ABS, ESP, ISOFIX, иммобилайзер, гарантия, центральный замок, "
                  "бортовой компьютер, подушки безопасности, подушка безопасности, "
                  "усилитель руля, противобуксовочная",
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
    "customs_kf_minsk": ("Коэффициент таможни (Минск)", "×",  "tariffs"),
    "invoice_pct":      ("Инвойс, процент",            "%",  "tariffs"),
    "invoice_fix":      ("Инвойс, фикс. часть",        "€",  "tariffs"),
    "extra_fix":        ("Прочие расходы",             "€",  "tariffs"),
    "buyback_min_eur":  ("Минимальный выкуп",          "€",  "tariffs"),
    "kp_exclude":       ("Стоп-слова комплектации",     "",   "kp", "text"),
    "kp_country":       ("Страна (в форме «из …»)",      "",   "kp", "text"),
    "kp_delivery":      ("Срок доставки (Минск/Культ40)", "",  "kp", "text"),
    "kp_delivery_msk":  ("Срок доставки (ЕС-МСК)",       "",   "kp", "text"),
    "img_count":        ("Сколько фото в КП",          "шт", "photo"),
    "img_step":         ("Шаг выборки фото",           "",   "photo"),
    "img_offset":       ("Пропустить первых фото",     "шт", "photo"),
}

SECTION_TITLES = {
    "rates":   "Курс дня",
    "tariffs": "Тарифы",
    "kp":      "Комплектация",
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
        # Снимок каждого отправленного КП. Клиентский бот находит его по токену
        # из подписи и строит презентацию из полных данных, а не из текста КП
        con.execute("""
            CREATE TABLE IF NOT EXISTS kp_snapshots (
                token       TEXT PRIMARY KEY,
                car_num     TEXT NOT NULL,
                direction   TEXT NOT NULL DEFAULT '',
                price_rub   INTEGER NOT NULL,
                data_json   TEXT NOT NULL,
                options_json TEXT NOT NULL,
                photos_json TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS kp_snapshots_lot ON kp_snapshots(car_num)")
        # Контрагенты клиентского бота: логотип и последняя наценка
        con.execute("""
            CREATE TABLE IF NOT EXISTS client_profiles (
                user_id    INTEGER PRIMARY KEY,
                logo_path  TEXT NOT NULL DEFAULT '',
                markup_rub INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        # Выбранный дизайн презентации — колонка появилась позже таблицы,
        # на рабочей базе её добавляем, не трогая данные
        try:
            con.execute("ALTER TABLE client_profiles ADD COLUMN design TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass                                    # уже есть
        # Задание на презентацию: что собрать, с какой наценкой и где какое фото
        con.execute("""
            CREATE TABLE IF NOT EXISTS client_jobs (
                id          TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                token       TEXT NOT NULL,
                formats     TEXT NOT NULL,
                markup_rub  INTEGER NOT NULL DEFAULT 0,
                with_logo   INTEGER NOT NULL DEFAULT 1,
                layout_json TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'new',
                created_at  TEXT NOT NULL
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

def setting_kind(key: str) -> str:
    """Тип значения настройки: number (по умолчанию) или text."""
    item = EDITABLE_SETTINGS.get(key)
    return item[3] if item and len(item) > 3 else "number"


def get_exclude_words() -> list[str]:
    """
    Стоп-слова комплектации. Опция с таким словом в КП не попадает:
    «иммобилайзер» и «ABS» есть у любой машины и только занимают место.
    """
    raw = get_setting("kp_exclude") or ""
    return [w.strip() for w in re.split(r"[,;\n]", raw) if w.strip()]


def get_tariffs() -> dict:
    return {
        "logistics_minsk":  get_float("logistics_minsk"),
        "logistics_kult40": get_float("logistics_kult40"),
        "logistics_msk":    get_float("logistics_msk"),
        "broker_rub":       get_float("broker_rub"),
        "util_fixed_rub":   get_float("util_fixed_rub"),
        "epts_rub":         get_float("epts_rub"),
        "customs_kf_minsk": get_float("customs_kf_minsk"),
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


# ── Ключ OpenRouter ──────────────────────────────────────────────────────────
# Хранится в базе, чтобы админ менял его из мини-аппа без деплоя. Ключ из .env
# остаётся запасным: пока в базе пусто, работает он. Наружу ключ целиком
# не отдаётся никогда — только маска.

_OPENROUTER_KEY = "openrouter_api_key"


def get_openrouter_key() -> str:
    """Действующий ключ: из настроек, иначе из окружения."""
    return get_optional(_OPENROUTER_KEY) or os.getenv("OPENROUTER_API_KEY", "")


def set_openrouter_key(key: str) -> None:
    set_setting(_OPENROUTER_KEY, key.strip())


def delete_openrouter_key() -> None:
    with _conn() as con:
        con.execute("DELETE FROM settings WHERE key=?", (_OPENROUTER_KEY,))


def mask_key(key: str) -> str:
    """«sk-or-v1-29f0…dfb1» — видно, какой ключ стоит, но не сам ключ."""
    key = key or ""
    if len(key) <= 16:
        return "•" * len(key)
    return f"{key[:10]}…{key[-4:]}"


def openrouter_key_info() -> dict:
    """Что показываем в настройках: задан ли ключ, откуда и его маску."""
    own = get_optional(_OPENROUTER_KEY)
    env = os.getenv("OPENROUTER_API_KEY", "")
    key = own or env
    return {
        "set": bool(key),
        "source": "settings" if own else ("env" if env else ""),
        "masked": mask_key(key) if key else "",
    }


# ── Снимки КП ────────────────────────────────────────────────────────────────

def new_snapshot_token() -> str:
    return secrets.token_urlsafe(12)


def save_snapshot(token: str, car_num, direction: str, price_rub: int, data: dict,
                  options: list[str], photos: dict) -> None:
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO kp_snapshots
               (token, car_num, direction, price_rub, data_json, options_json, photos_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (token, str(car_num), direction or "", int(price_rub),
             json.dumps(data, ensure_ascii=False, default=str),
             json.dumps(options, ensure_ascii=False),
             json.dumps(photos, ensure_ascii=False),
             datetime.now().strftime("%d.%m.%Y %H:%M")),
        )


def _snapshot_row(row) -> dict:
    return {
        "token": row["token"], "car_num": row["car_num"], "direction": row["direction"],
        "price_rub": row["price_rub"], "created_at": row["created_at"],
        "data": json.loads(row["data_json"]),
        "options": json.loads(row["options_json"]),
        "photos": json.loads(row["photos_json"]),
    }


def get_snapshot(token: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM kp_snapshots WHERE token=?", (token,)).fetchone()
    return _snapshot_row(row) if row else None


def find_snapshot(car_num: str, price_rub: int) -> dict | None:
    """
    Запасной поиск, когда ссылки с токеном в подписи нет. Номер лота
    не уникален между листами, поэтому только в паре с ценой.
    """
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM kp_snapshots WHERE car_num=? AND price_rub=? "
            "ORDER BY rowid DESC LIMIT 1", (str(car_num), int(price_rub)),
        ).fetchone()
    return _snapshot_row(row) if row else None


# ── Клиентский бот: профили и задания ────────────────────────────────────────

def get_client_profile(user_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM client_profiles WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def _upsert_profile(user_id: int, **fields) -> None:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO client_profiles (user_id, updated_at) VALUES (?, ?)",
            (user_id, now))
        for key, value in fields.items():
            con.execute(f"UPDATE client_profiles SET {key}=?, updated_at=? WHERE user_id=?",
                        (value, now, user_id))


def save_client_logo(user_id: int, path: str) -> None:
    _upsert_profile(user_id, logo_path=path)


def save_client_markup(user_id: int, markup_rub: int) -> None:
    _upsert_profile(user_id, markup_rub=int(markup_rub))


def save_client_design(user_id: int, design: str) -> None:
    _upsert_profile(user_id, design=design)


def create_job(user_id: int, chat_id: int, token: str, formats: list[str],
               markup_rub: int, layout: dict, with_logo: bool = True) -> str:
    job_id = secrets.token_urlsafe(16)
    with _conn() as con:
        con.execute(
            """INSERT INTO client_jobs
               (id, user_id, chat_id, token, formats, markup_rub, with_logo, layout_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
            (job_id, user_id, chat_id, token, ",".join(formats), int(markup_rub), int(with_logo),
             json.dumps(layout, ensure_ascii=False), datetime.now().strftime("%d.%m.%Y %H:%M")),
        )
    return job_id


def get_job(job_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM client_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    job = dict(row)
    job["formats"] = [f for f in job["formats"].split(",") if f]
    job["layout"] = json.loads(job.pop("layout_json") or "{}")
    return job


def update_job_layout(job_id: str, layout: dict) -> None:
    with _conn() as con:
        con.execute("UPDATE client_jobs SET layout_json=? WHERE id=?",
                    (json.dumps(layout, ensure_ascii=False), job_id))


def set_job_status(job_id: str, status: str) -> None:
    with _conn() as con:
        con.execute("UPDATE client_jobs SET status=? WHERE id=?", (status, job_id))
