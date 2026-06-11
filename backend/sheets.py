"""
Google Sheets integration via gspread service account.
Writes a new car row (A–W) to the configured sheet tab.
"""
import os
import time
import logging
from datetime import datetime
from pathlib import Path

import gspread
from dotenv import load_dotenv

log = logging.getLogger(__name__)

_RETRY_EXCEPTIONS = (
    gspread.exceptions.APIError,
    OSError,           # includes SSLError, ConnectionResetError
)
_RETRY_ATTEMPTS = 3
_RETRY_PAUSE    = 5   # seconds between retries


def _with_retry(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) up to _RETRY_ATTEMPTS times on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except _RETRY_EXCEPTIONS as e:
            last_exc = e
            log.warning("Sheets API error (attempt %d/%d): %s", attempt + 1, _RETRY_ATTEMPTS, e)
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_PAUSE)
    raise last_exc

load_dotenv(Path(__file__).parent.parent / ".env")

SHEET_ID   = os.getenv("GOOGLE_SHEET_ID",       "")
SHEET_TAB  = os.getenv("GOOGLE_SHEET_TAB",      "B2B (ЕС/Минск)")
KULT40_TAB = os.getenv("GOOGLE_SHEET_KULT40",   "B2B (ЕС/Культ40)")
MSK_TAB    = os.getenv("GOOGLE_SHEET_MSK",       "B2B (ЕС-МСК)")
CREDS_FILE = Path(__file__).parent / os.getenv("GOOGLE_CREDENTIALS", "credentials.json")

_client: gspread.Client | None = None


def _get_client() -> gspread.Client:
    global _client
    if _client is None:
        _client = gspread.service_account(filename=str(CREDS_FILE))
    return _client


def _get_worksheet() -> gspread.Worksheet:
    gc = _get_client()
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet(SHEET_TAB)


def _get_kult40_worksheet() -> gspread.Worksheet:
    gc = _get_client()
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet(KULT40_TAB)


def _get_msk_worksheet() -> gspread.Worksheet:
    gc = _get_client()
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet(MSK_TAB)


def get_next_row_number(ws: gspread.Worksheet) -> tuple[int, int]:
    """
    Returns (car_number, sheet_row_index).
    car_number = last integer in column A + 1
    sheet_row  = first row where BOTH column A AND column G are empty
                 (handles cases where A has a bad date value instead of a number)
    """
    col_a = _with_retry(ws.col_values, 1)   # column A (№)
    col_g = _with_retry(ws.col_values, 7)   # column G (БРУТТО EUR)

    # Find the last row that has an integer car number in A
    last_num      = 0
    last_num_row  = 1
    for i, v in enumerate(col_a):
        try:
            n = int(v)
            last_num     = n
            last_num_row = i + 1   # 0-based → 1-based
        except (ValueError, TypeError):
            continue

    # Find first row (after header) where G is empty — this is truly free
    first_empty_g = len(col_g) + 1   # default: row after last G value
    for i, v in enumerate(col_g):
        if i == 0:            # skip header row
            continue
        if not str(v).strip():
            first_empty_g = i + 1   # 0-based → 1-based
            break

    # Use the greater of the two to avoid overwriting existing data
    next_row = max(last_num_row + 1, first_empty_g)
    return last_num + 1, next_row


def append_car_row(data: dict) -> int:
    """
    Writes data columns to the sheet row, leaving formula columns (H, L, M, O, Q, T, V) untouched.
    data keys: counterparty, url, car_name, price_eur, vat, buyback_pct,
               rate_n, rate_p, r_value, customs_eur, util_rub
    Returns the assigned car number (column A).
    """
    ws = _get_worksheet()
    car_num, r = get_next_row_number(ws)

    # Force column A cell to Number format — template rows may have Date format
    # which would turn e.g. 1060 → "25.11.1902" (date serial)
    _with_retry(ws.format, f"A{r}", {"numberFormat": {"type": "NUMBER", "pattern": "0"}})

    pct = data.get("buyback_pct", 6)
    factor = f"{1 + pct / 100:.2f}".replace(".", ",")  # e.g. 10% → "1,10"

    _with_retry(ws.batch_update,  # type: ignore[arg-type]
        [
            # A–G: №, контрагент, дата, авто, ссылка, VIN (пусто), БРУТТО EUR
            {"range": f"A{r}:G{r}", "values": [[
                car_num,
                data.get("counterparty", ""),
                datetime.today().strftime("%d.%m.%Y"),
                data.get("car_name", ""),
                data.get("url", ""),
                "",
                data.get("price_eur", 0),
            ]]},
            # H: НЕТТО — absolute ref to $I$2 (global VAT cell), matching sheet template
            {"range": f"H{r}", "values": [[f"=G{r}/$I$2"]]},
            # I: НДС (per-row, informational)
            {"range": f"I{r}", "values": [[data.get("vat", 1.19)]]},
            # J: логистика (фиксировано 5300)
            {"range": f"J{r}", "values": [[5300]]},
            # K: выкуп — dynamic formula with manager's chosen %
            {"range": f"K{r}", "values": [[f"=H{r}*{factor}-H{r}"]]},
            # L: инвойс
            {"range": f"L{r}", "values": [[f"=(H{r}*1,013+100)-H{r}"]]},
            # M: итого EUR
            {"range": f"M{r}", "values": [[f"=H{r}+J{r}+K{r}+L{r}+350"]]},
            # N: курс EUR/USDT
            {"range": f"N{r}", "values": [[data.get("rate_n", 1.1621)]]},
            # O: итого USDT
            {"range": f"O{r}", "values": [[f"=M{r}*N{r}"]]},
            # P: курс USDT/₽
            {"range": f"P{r}", "values": [[data.get("rate_p", 79.7)]]},
            # Q: итого ₽
            {"range": f"Q{r}", "values": [[f"=O{r}*P{r}"]]},
            # R: ЭПТС/СБКТС, S: таможня EUR
            {"range": f"R{r}:S{r}", "values": [[
                data.get("r_value", 3500),
                data.get("customs_eur", "") or "",
            ]]},
            # T: таможня ₽
            {"range": f"T{r}", "values": [[f"=S{r}*N{r}*P{r}"]]},
            # U: утиль ₽
            {"range": f"U{r}", "values": [[data.get("util_rub", "") or ""]]},
            # V: итого с утильсбором
            {"range": f"V{r}", "values": [[f"=Q{r}+R{r}+T{r}+U{r}"]]},
        ],
        value_input_option="USER_ENTERED",
    )
    return car_num, r          # (car_num, sheet_row)


def _update_minsk_row(sheet_row: int, data: dict) -> None:
    """Re-write editable cells in a Минск row (after history edit)."""
    ws = _get_worksheet()
    r = sheet_row
    pct = data.get("buyback_pct", 6)
    factor = f"{1 + pct / 100:.2f}".replace(".", ",")
    _with_retry(ws.batch_update,  # type: ignore[arg-type]
        [
            {"range": f"B{r}",   "values": [[data.get("counterparty", "")]]},
            {"range": f"I{r}",   "values": [[data.get("vat", 1.19)]]},
            {"range": f"K{r}",   "values": [[f"=H{r}*{factor}-H{r}"]]},
            {"range": f"S{r}",   "values": [[data.get("customs_eur", "") or ""]]},
            {"range": f"U{r}",   "values": [[data.get("util_rub", "") or ""]]},
        ],
        value_input_option="USER_ENTERED",
    )


def append_kult40_row(data: dict) -> tuple[int, int]:
    """Write a new row to B2B (ЕС/Культ40). Returns (car_num, sheet_row)."""
    ws = _get_kult40_worksheet()
    car_num, r = get_next_row_number(ws)

    _with_retry(ws.format, f"A{r}", {"numberFormat": {"type": "NUMBER", "pattern": "0"}})

    pct    = data.get("buyback_pct", 8)
    factor = f"{1 + pct / 100:.2f}".replace(".", ",")

    _with_retry(ws.batch_update,
        [
            # A–E: №, контрагент, дата, авто, ссылка
            {"range": f"A{r}:E{r}", "values": [[
                car_num,
                data.get("counterparty", ""),
                datetime.today().strftime("%d.%m.%Y"),
                data.get("car_name", ""),
                data.get("url", ""),
            ]]},
            {"range": f"F{r}", "values": [[data.get("price_eur", 0)]]},          # БРУТТО
            {"range": f"G{r}", "values": [[f"=F{r}/H{r}"]]},                     # НЕТТО
            {"range": f"H{r}", "values": [[data.get("vat", 1.19)]]},              # НДС
            {"range": f"I{r}", "values": [[data.get("logistics", 4100)]]},        # Логистика
            {"range": f"J{r}", "values": [[f"=G{r}*{factor}-G{r}"]]},            # Выкуп
            {"range": f"K{r}", "values": [[f"=(G{r}*1,013+100)-G{r}"]]},         # Инвойс
            {"range": f"L{r}", "values": [[f"=G{r}+I{r}+J{r}+K{r}+350"]]},      # Итого EUR
            {"range": f"M{r}", "values": [[data.get("rate_eur_usd", 1.10)]]},    # КК EUR/USD
            {"range": f"N{r}", "values": [[f"=L{r}*M{r}"]]},                     # USDT
            {"range": f"O{r}", "values": [[data.get("rate_usd_rub", 80.0)]]},    # USD/RUB
            {"range": f"P{r}", "values": [[f"=N{r}*O{r}"]]},                     # RUB
            {"range": f"Q{r}", "values": [[100_000]]},                            # Брокер (fixed)
            {"range": f"R{r}", "values": [[data.get("evacuator_rub", 0) or 0]]}, # Эвакуатор
            {"range": f"S{r}", "values": [[data.get("customs_tks_rub", 0) or 0]]}, # Таможня ТКС
            {"range": f"T{r}", "values": [[5_200]]},                              # Утиль (fixed)
            {"range": f"U{r}", "values": [[f"=P{r}+Q{r}+R{r}+S{r}+T{r}"]]},    # Итого
        ],
        value_input_option="USER_ENTERED",
    )
    return car_num, r


def append_msk_row(data: dict) -> tuple[int, int]:
    """Write a new row to B2B (ЕС-МСК). Returns (car_num, sheet_row)."""
    ws = _get_msk_worksheet()
    car_num, r = get_next_row_number(ws)

    _with_retry(ws.format, f"A{r}", {"numberFormat": {"type": "NUMBER", "pattern": "0"}})

    pct    = data.get("buyback_pct", 8)
    factor = f"{1 + pct / 100:.2f}".replace(".", ",")

    _with_retry(ws.batch_update,
        [
            {"range": f"A{r}:E{r}", "values": [[
                car_num,
                data.get("counterparty", ""),
                datetime.today().strftime("%d.%m.%Y"),
                data.get("car_name", ""),
                data.get("url", ""),
            ]]},
            {"range": f"F{r}", "values": [[data.get("price_eur", 0)]]},
            {"range": f"G{r}", "values": [[f"=F{r}/H{r}"]]},
            {"range": f"H{r}", "values": [[data.get("vat", 1.19)]]},
            {"range": f"I{r}", "values": [[data.get("logistics", 2500)]]},
            {"range": f"J{r}", "values": [[f"=G{r}*{factor}-G{r}"]]},
            {"range": f"K{r}", "values": [[f"=(G{r}*1,013+100)-G{r}"]]},
            {"range": f"L{r}", "values": [[f"=G{r}+I{r}+J{r}+K{r}+350"]]},
            {"range": f"M{r}", "values": [[data.get("rate_eur_usd", 1.10)]]},
            {"range": f"N{r}", "values": [[f"=L{r}*M{r}"]]},
            {"range": f"O{r}", "values": [[data.get("rate_usd_rub", 80.0)]]},
            {"range": f"P{r}", "values": [[f"=N{r}*O{r}"]]},
            {"range": f"Q{r}", "values": [[100_000]]},                            # Брокер (fixed)
            {"range": f"R{r}", "values": [[data.get("customs_tks_rub", 0) or 0]]}, # Таможня ТКС
            {"range": f"S{r}", "values": [[5_200]]},                              # Утиль (fixed)
            {"range": f"T{r}", "values": [[f"=P{r}+Q{r}+R{r}+S{r}"]]},          # Итого
        ],
        value_input_option="USER_ENTERED",
    )
    return car_num, r


def update_kult40_row(sheet_row: int, data: dict) -> None:
    """Re-write editable cells in a Культ40 row after history edit."""
    ws = _get_kult40_worksheet()
    r  = sheet_row
    pct    = data.get("buyback_pct", 8)
    factor = f"{1 + pct / 100:.2f}".replace(".", ",")
    _with_retry(ws.batch_update,
        [
            {"range": f"B{r}", "values": [[data.get("counterparty", "")]]},
            {"range": f"H{r}", "values": [[data.get("vat", 1.19)]]},
            {"range": f"J{r}", "values": [[f"=G{r}*{factor}-G{r}"]]},
            {"range": f"R{r}", "values": [[data.get("evacuator_rub", 0) or 0]]},
            {"range": f"S{r}", "values": [[data.get("customs_tks_rub", 0) or 0]]},
        ],
        value_input_option="USER_ENTERED",
    )


def update_msk_row(sheet_row: int, data: dict) -> None:
    """Re-write editable cells in an МСК row after history edit."""
    ws = _get_msk_worksheet()
    r  = sheet_row
    pct    = data.get("buyback_pct", 8)
    factor = f"{1 + pct / 100:.2f}".replace(".", ",")
    _with_retry(ws.batch_update,
        [
            {"range": f"B{r}", "values": [[data.get("counterparty", "")]]},
            {"range": f"H{r}", "values": [[data.get("vat", 1.19)]]},
            {"range": f"J{r}", "values": [[f"=G{r}*{factor}-G{r}"]]},
            {"range": f"R{r}", "values": [[data.get("customs_tks_rub", 0) or 0]]},
        ],
        value_input_option="USER_ENTERED",
    )


def update_car_row(sheet_row: int, data: dict) -> None:
    """Direction-aware update: routes to the correct sheet."""
    direction = data.get("direction", "minsk")
    if direction == "kult40":
        update_kult40_row(sheet_row, data)
    elif direction == "msk":
        update_msk_row(sheet_row, data)
    else:
        _update_minsk_row(sheet_row, data)


def test_connection() -> str:
    """Quick connectivity test — returns sheet title."""
    ws = _get_worksheet()
    return ws.title
