"""
SQLite storage for bot settings and pending (incomplete) car requests.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot_data.db"

_DEFAULTS = {
    # ── ЕС/Минск (sheet 1) ────────────────────────────────────────────────────
    "rate_n":           "1.1621",  # EUR → USDT  (column N)
    "rate_p":           "79.7",    # USDT → RUB  (column P)
    "r_value":          "45000",   # ЭПТС/СБКТС  (column R, руб)
    # ── ЕС/Культ40 + ЕС-МСК (sheets 2 & 3) ──────────────────────────────────
    "rate_eur_usd":     "1.1000",  # EUR/USD КК  (column M)
    "rate_usd_rub":     "80.0",    # USD/RUB ЦБ  (column O)
    "logistics_kult40": "4100",    # логистика ЕС/Культ40 (column I)
    "logistics_msk":    "2500",    # логистика ЕС-МСК     (column I)
    # ── фото ──────────────────────────────────────────────────────────────────
    "img_offset":       "0",
    "img_count":        "6",
}


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _conn() as con:
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
        # Seed defaults (ignore if already set)
        for k, v in _DEFAULTS.items():
            con.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str) -> str:
    with _conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else _DEFAULTS.get(key, "0")


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
