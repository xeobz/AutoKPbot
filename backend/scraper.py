"""
Парсер объявлений: mobile.de (все локали) + autoscout24 (.ru/.com/.de/…).

Публичная точка входа — scrape(url). Возвращает единый словарь:
    photos, all_photos, price_eur, make, model, title, year, mileage,
    color, features, power_hp, engine_l, fuel, gearbox
"""
import re

from bs4 import BeautifulSoup

_FEAT_RE = re.compile(r'\\"li\\",\\"([^\\"]{2,80})\\",\{\\"className\\":\\"CheckList')
_ATTR_RE = re.compile(r'\\"tag\\":\\"([^\\"]+)\\",\\"value\\":\\"([^\\"]+)\\"')
# Полное название объявления: ...,"title":"Volkswagen Tiguan Elegance 1,5 l eHybrid","url":"https://suchen.mobile.de/...
_TITLE_RE = re.compile(
    r'\\"title\\":\\"([^\\"]{5,140})\\",\\"url\\":\\"https://[\w.\-]*mobile\.de'
)


# ── Определение источника и нормализация ссылок ──────────────────────────────

MOBILE_DE_RE = re.compile(r"https?://[\w.\-]*mobile\.de/[^\s<>\"']+", re.IGNORECASE)
AUTOSCOUT_RE = re.compile(
    r"https?://[\w.\-]*autoscout24\.[a-z]{2,3}(?:\.[a-z]{2,3})?/[^\s<>\"']+", re.IGNORECASE
)

# id объявления mobile.de: ?id=459974296  или  /auto-inserat/<slug>/459974296.html
_MDE_ID_QUERY = re.compile(r"[?&]id=(\d{6,})")
_MDE_ID_PATH  = re.compile(r"/(\d{6,})\.html")

# Канонический русский URL детальной страницы (транспортные-средства/подробности.html)
_MDE_RU_URL = (
    "https://www.mobile.de/ru/"
    "%D1%82%D1%80%D0%B0%D0%BD%D1%81%D0%BF%D0%BE%D1%80%D1%82%D0%BD%D1%8B%D0%B5-"
    "%D1%81%D1%80%D0%B5%D0%B4%D1%81%D1%82%D0%B2%D0%B0/"
    "%D0%BF%D0%BE%D0%B4%D1%80%D0%BE%D0%B1%D0%BD%D0%BE%D1%81%D1%82%D0%B8.html?id={id}"
)

SOURCE_MOBILE_DE  = "mobile_de"
SOURCE_AUTOSCOUT  = "autoscout24"


def normalise_mobile_de(url: str) -> str:
    """
    Любую ссылку mobile.de приводим к русской детальной странице.
    Поддерживаются форматы:
      • https://www.mobile.de/ru/…/подробности.html?id=459974296
      • https://suchen.mobile.de/fahrzeuge/details.html?id=459974296
      • https://suchen.mobile.de/auto-inserat/<slug>/459974296.html   (новый)
    Если id не нашли — возвращаем ссылку как есть (браузер сам отдаст /ru,
    т.к. заходим с русской локалью).
    """
    m = _MDE_ID_QUERY.search(url) or _MDE_ID_PATH.search(url)
    if m:
        return _MDE_RU_URL.format(id=m.group(1))
    return url


def detect_source(url: str) -> str | None:
    if MOBILE_DE_RE.match(url) or "mobile.de/" in url.lower():
        return SOURCE_MOBILE_DE
    if AUTOSCOUT_RE.match(url) or "autoscout24." in url.lower():
        return SOURCE_AUTOSCOUT
    return None


def find_listing_url(text: str) -> tuple[str, str] | None:
    """
    Ищет в тексте ссылку на объявление. Возвращает (нормализованный_url, источник)
    или None. Первой проверяется mobile.de, затем autoscout24.
    """
    m = MOBILE_DE_RE.search(text or "")
    if m:
        return normalise_mobile_de(m.group(0)), SOURCE_MOBILE_DE
    m = AUTOSCOUT_RE.search(text or "")
    if m:
        return m.group(0), SOURCE_AUTOSCOUT
    return None


# ── Нормализация значений (общая для обоих источников) ───────────────────────

def normalise_fuel(raw: str) -> str:
    """«Гибрид (бензин/электричество), Подключаемый гибрид» → «Гибрид»."""
    s = (raw or "").lower()
    if not s:
        return ""
    if "гибрид" in s or "hybrid" in s or "/бензин" in s or "бензин/" in s:
        return "Гибрид"
    if "дизель" in s or "diesel" in s:
        return "Дизель"
    if "электро" in s or "electric" in s or "elektro" in s:
        return "Электро"
    if any(g in s for g in ("газ", "lpg", "cng", "автогаз")):
        return "Газ"
    if "бензин" in s or "petrol" in s or "benzin" in s or "super" in s or "95" in s:
        return "Бензин"
    return raw.split(",")[0].strip()


def normalise_gearbox(raw: str) -> str:
    s = (raw or "").lower()
    if not s:
        return ""
    if "автомат" in s or "automat" in s or "dsg" in s:
        return "Автомат"
    if "механ" in s or "manual" in s or "schalt" in s:
        return "Механика"
    return raw.strip()


def parse_engine_litres(*sources: str) -> float | None:
    """Достаёт объём двигателя: «1,5 l», «2.0 TDI», «1 395 см³» → 1.5 / 2.0 / 1.4."""
    for src in sources:
        if not src:
            continue
        # «1 395 см³» / «1395 ccm»
        m = re.search(r"(\d[\d\s\xa0]{2,6})\s*(?:см³|ccm|cm³|cc)\b", src, re.IGNORECASE)
        if m:
            ccm = int(re.sub(r"\D", "", m.group(1)))
            if 600 <= ccm <= 9000:
                return round(ccm / 1000, 1)
        # «1,5 l» / «2.0 TDI» / «1.5 TSI»
        m = re.search(r"\b(\d[.,]\d)\s*(?:l\b|л\b|liter|литр|tsi|tdi|tfsi|dci|cdi|hdi)", src, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def parse_power_hp(*sources: str) -> int | None:
    """«110 кВт (150 л. с.)» → 150; «245 л.с.» → 245; «150 PS» → 150."""
    for src in sources:
        if not src:
            continue
        s = src.replace("\xa0", " ")
        m = re.search(r"(\d{2,4})\s*(?:л\.?\s?с\.?|hp|ps|bhp)", s, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


# ── mobile.de: разбор RSC-пейлоада ───────────────────────────────────────────

def _collect_rsc(soup: BeautifulSoup) -> str:
    parts = []
    for tag in soup.find_all("script", src=False):
        txt = tag.string or ""
        if "self.__next_f" in txt:
            parts.append(txt)
    return "\n".join(parts)


def _extract_price(rsc: str) -> float | None:
    m = re.search(r'"price"\s*:\s*"([\d\s\xa0 ,\.]+)\s*€"', rsc)
    if m:
        raw = re.sub(r"[\s\xa0 ,\.]", "", m.group(1))
        try:
            return float(raw)
        except ValueError:
            pass
    return None


def _extract_title_info(soup: BeautifulSoup) -> tuple[str, str, float | None]:
    title = soup.title.string if soup.title else ""
    m = re.match(r"^(.+?)\s+для\s+([\d\s\xa0 \.]+)\s*€", title)
    if m:
        name = m.group(1).strip()
        parts = name.split(None, 1)
        make = parts[0]
        model = parts[1] if len(parts) > 1 else ""
        raw = re.sub(r"[\s\xa0 \.]", "", m.group(2))
        try:
            price = float(raw)
        except ValueError:
            price = None
        return make, model, price
    return "", "", None


def _extract_full_title(rsc: str) -> str:
    """Полное название объявления: «Volkswagen Tiguan Elegance 1,5 l eHybrid 6-Gang-DSG»."""
    m = _TITLE_RE.search(rsc)
    if m:
        return re.sub(r"\s{2,}", " ", m.group(1)).strip()
    return ""


def _uid_to_url(uid: str) -> str:
    return f"https://img.classistatic.de/api/v1/mo-prod/images/{uid[:2]}/{uid}?rule=mo-640.jpg"


def _extract_images(rsc: str) -> list[str]:
    """
    Фото галереи: все ссылки на картинки ищутся с их позициями в тексте.
    Галерея — самый плотный кластер (массив из 10-50 ссылок рядом),
    логотипы дилера и иконки стоят особняком. Берём самый большой кластер.
    """
    uuid_re = re.compile(
        r"img\.classistatic\.de/api/v1/mo-prod/images/[a-f0-9]{2}/([a-f0-9\-]{36})"
    )
    matches = list(uuid_re.finditer(rsc))
    if not matches:
        return []

    GAP = 800   # символов; ссылки одного JSON-массива лежат гораздо ближе
    groups: list[list[re.Match]] = []
    cur: list[re.Match] = [matches[0]]
    for m in matches[1:]:
        if m.start() - cur[-1].end() < GAP:
            cur.append(m)
        else:
            groups.append(cur)
            cur = [m]
    groups.append(cur)

    gallery = max(groups, key=len)

    seen: set[str] = set()
    urls: list[str] = []
    for m in gallery:
        uid = m.group(1)
        if uid not in seen:
            seen.add(uid)
            urls.append(_uid_to_url(uid))
    return urls


def _extract_all_images_flat(rsc: str) -> list[str]:
    """Все картинки страницы по порядку — используется только отладкой."""
    uuid_re = re.compile(
        r"img\.classistatic\.de/api/v1/mo-prod/images/[a-f0-9]{2}/([a-f0-9\-]{36})"
    )
    seen: set[str] = set()
    urls: list[str] = []
    for m in uuid_re.finditer(rsc):
        uid = m.group(1)
        if uid not in seen:
            seen.add(uid)
            urls.append(_uid_to_url(uid))
    return urls


def _extract_features(rsc: str) -> list[str]:
    """
    Комплектация машины из чек-листа объявления.

    Той же разметкой на странице свёрстан блок «Дополнительные услуги» дилера
    (тест-драйв, шиномонтаж, лизинг). Его пункты отличаются классом
    listItemReverse — иначе «Окрасочный цех» уезжает в КП как опция.
    """
    names: list[str] = []
    for m in _FEAT_RE.finditer(rsc):
        class_attr = rsc[m.end() : m.end() + 90]     # хвост className после «CheckList»
        if "listItemReverse" in class_attr:
            continue
        names.append(m.group(1))
    return list(dict.fromkeys(names))


def _extract_attributes(rsc: str) -> dict:
    attrs: dict[str, str] = {}
    for tag, value in _ATTR_RE.findall(rsc):
        attrs.setdefault(tag, value)
    return attrs


def _parse_year(attrs: dict) -> str:
    for key in ("firstRegistration", "registrationDate", "modelYear", "year"):
        val = attrs.get(key, "")
        if val:
            m = re.search(r"\d{4}", val)
            if m:
                return m.group(0)
    return ""


def _parse_mileage(attrs: dict) -> int | None:
    for key in ("mileage", "mileageInKm", "km"):
        val = attrs.get(key, "")
        if val:
            digits = re.sub(r"\D", "", val)
            if digits:
                return int(digits)
    return None


def _parse_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    rsc = _collect_rsc(soup)
    make, model, title_price = _extract_title_info(soup)
    price_eur = _extract_price(rsc) or title_price
    attrs = _extract_attributes(rsc)
    year = _parse_year(attrs)
    mileage = _parse_mileage(attrs)
    color = attrs.get("color", attrs.get("exteriorColor", attrs.get("paintColor", "")))
    if not make:
        make = attrs.get("make", "")
    if not model:
        model = attrs.get("model", "")

    full_title = _extract_full_title(rsc) or f"{make} {model}".strip()

    return {
        "source":     SOURCE_MOBILE_DE,
        "photos":     _extract_images(rsc),          # только кластер галереи
        "all_photos": _extract_all_images_flat(rsc), # все картинки — для отладки
        "price_eur": price_eur,
        "make": make,
        "model": model,
        "title": full_title,
        "year": year,
        "mileage": mileage,
        "color": color,
        "features": _extract_features(rsc),
        "power_hp": parse_power_hp(attrs.get("power", "")),
        "engine_l": parse_engine_litres(
            attrs.get("cubicCapacity", ""), attrs.get("displacement", ""), full_title
        ),
        "fuel": normalise_fuel(attrs.get("fuel", "")),
        "gearbox": normalise_gearbox(attrs.get("transmission", "")),
    }


# ── Загрузка страницы браузером (nodriver) ───────────────────────────────────

def content_ok(html: str, ready_markers: tuple[str, ...],
               blocked_markers: tuple[str, ...]) -> bool:
    """Страница пришла целиком: нужные куски на месте, следов блокировки нет."""
    if not html:
        return False
    if any(b in html for b in blocked_markers):
        return False
    return all(m in html for m in ready_markers) if ready_markers else True


async def _fetch_html_async(url: str, warmup_url: str, blocked_markers: tuple[str, ...],
                            profile_dir: str | None = None,
                            ready_markers: tuple[str, ...] = ()) -> str:
    import asyncio
    import nodriver as uc

    import sys
    headless = sys.platform != "win32"
    # user_data_dir задаём сами: профиль весит ~56 МБ, и если его создаст
    # nodriver, каталог останется после жёсткого выхода процесса.
    # sandbox=False обязателен, потому что сервис работает от root.
    browser = await uc.start(
        headless=headless,
        sandbox=False,
        user_data_dir=profile_dir,
        browser_args=["--lang=ru-RU", "--no-sandbox", "--disable-dev-shm-usage"],
    )
    import time as _time

    try:
        # Прогрев: сначала главная — Cloudflare ставит куки и решает JS-челлендж,
        # заодно фиксируется русская локаль сайта.
        if warmup_url:
            await browser.get(warmup_url)
            await asyncio.sleep(1.5)

        html = ""
        for attempt in range(2):
            page = await browser.get(url)
            # Ждём появления данных, а не «на всякий случай»: страница обычно
            # готова за секунду-две, фиксированная пауза просто тратила время
            deadline = _time.monotonic() + 25
            while _time.monotonic() < deadline:
                html = await page.get_content()
                if content_ok(html, ready_markers, blocked_markers):
                    break
                await asyncio.sleep(0.4)
            if content_ok(html, ready_markers, blocked_markers):
                break
            if attempt == 0:
                await asyncio.sleep(3)   # пауза перед второй попыткой
    finally:
        browser.stop()

    if any(marker in html for marker in blocked_markers):
        raise Exception("Сайт отклонил запрос. Попробуй ещё раз через минуту.")

    return html


def _repair_non_utf8_sources(package: str = "nodriver") -> list[str]:
    """
    Чинит исходники пакета, которые не читаются как UTF-8.

    nodriver 0.50.3 поставляет cdp/network.py, где «±» записан одним байтом
    0xB1 (latin-1). Python 3 читает исходники только как UTF-8, поэтому
    `import nodriver` падает с SyntaxError, и парсер не работает вообще.
    На машинах, где рядом оказался готовый .pyc, проблема незаметна —
    поэтому её легко не увидеть при разработке.

    Заменяем одиночные «высокие» байты на их корректную UTF-8 запись,
    не трогая уже валидные последовательности. Возвращает список починенных
    файлов (пустой, если чинить нечего).
    """
    import importlib.util
    import os
    import tempfile
    from pathlib import Path as _Path

    try:
        spec = importlib.util.find_spec(package)      # не выполняет пакет
        if not spec or not spec.origin:
            return []
        root = _Path(spec.origin).parent
    except Exception:
        return []

    fixed: list[str] = []
    for path in root.rglob("*.py"):
        try:
            data = path.read_bytes()
            data.decode("utf-8")
            continue                                   # файл в порядке
        except UnicodeDecodeError:
            pass
        except OSError:
            continue

        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b < 0x80:
                out.append(b)
                i += 1
                continue
            for length in (2, 3, 4):                   # валидная UTF-8 последовательность?
                chunk = data[i:i + length]
                try:
                    chunk.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                out += chunk
                i += length
                break
            else:                                       # одиночный байт — это latin-1
                out += chr(b).encode("utf-8")
                i += 1

        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, delete=False, suffix=".tmp"
            ) as tmp:
                tmp.write(bytes(out))
                tmp_name = tmp.name
            os.replace(tmp_name, path)
            # старый байт-код мог остаться от прошлой версии файла
            cache = path.parent / "__pycache__"
            if cache.is_dir():
                for pyc in cache.glob(f"{path.stem}.*.pyc"):
                    pyc.unlink(missing_ok=True)
            fixed.append(str(path))
        except OSError:
            continue                                    # нет прав на запись — оставляем как есть

    return fixed


def _temp_base() -> str | None:
    """
    Где держать временные файлы браузера.

    На сервере /tmp смонтирован как tmpfs, то есть лежит в оперативной памяти,
    а профиль Chrome весит около 56 МБ — несколько разборов подряд съедали бы
    память машины. Поэтому на Linux уводим временные каталоги на диск.
    """
    import os
    import sys

    if sys.platform != "win32":
        for candidate in ("/var/tmp", "/opt/autokpbot/tmp"):
            if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
                return candidate
    return None      # системный каталог по умолчанию


def _sweep_stale_temp(base: str | None, max_age_sec: int = 3600) -> None:
    """Подчищает каталоги от прошлых запусков, если процесс не успел прибраться."""
    import shutil
    import tempfile
    import time
    from pathlib import Path as _Path

    root = _Path(base or tempfile.gettempdir())
    now = time.time()
    try:
        entries = list(root.glob("autokp_scrape_*"))
    except OSError:
        return
    for path in entries:
        try:
            if now - path.stat().st_mtime > max_age_sec:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _browser_worker(url: str, warmup_url: str, blocked_markers: tuple[str, ...],
                    out_path: str, err_path: str, profile_dir: str,
                    ready_markers: tuple[str, ...] = ()) -> None:
    """
    Тело отдельного процесса: поднимает браузер, сохраняет HTML в файл.

    Почему процесс, а не поток: nodriver после закрытия браузера оставляет
    фоновый слушатель, который в связке с новыми websockets валится в вечный
    цикл — 80% ядра и гигабайты логов. Поток так не убить, процесс — легко.
    Выходим через os._exit, чтобы не ждать зависшие потоки библиотеки.
    """
    import asyncio as _asyncio
    import logging as _logging
    import os as _os
    import sys as _sys
    from pathlib import Path as _Path

    # Библиотечный шум в journald не нужен
    for name in ("nodriver", "websockets", "uc", "asyncio"):
        _logging.getLogger(name).setLevel(_logging.CRITICAL)

    # Своя группа процессов: если браузер зависнет, родитель прибьёт всю ветку
    # целиком, иначе Chrome остаётся сиротой и продолжает держать память
    try:
        _os.setsid()
    except (AttributeError, OSError):
        pass

    code = 1
    try:
        # Библиотека может приехать с исходником, который Python не прочитает
        _repair_non_utf8_sources("nodriver")

        # На Windows нужен именно Proactor: Selector-цикл не умеет запускать
        # подпроцессы, и Chrome не стартует.
        if _sys.platform == "win32" and hasattr(_asyncio, "ProactorEventLoop"):
            loop = _asyncio.ProactorEventLoop()
        else:
            loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)

        html = loop.run_until_complete(
            _fetch_html_async(url, warmup_url, tuple(blocked_markers), profile_dir,
                              tuple(ready_markers))
        )
        _Path(out_path).write_text(html, encoding="utf-8")
        code = 0
    except Exception as e:
        try:
            _Path(err_path).write_text(f"{e}", encoding="utf-8")
        except Exception:
            pass
    finally:
        _sys.stderr.flush()
        _os._exit(code)      # немедленный выход, без ожидания потоков nodriver


FETCH_TIMEOUT = 150   # секунд на одну попытку


async def fetch_html(
    url: str,
    warmup_url: str = "https://www.mobile.de/ru/",
    blocked_markers: tuple[str, ...] = ("Access denied", "Zugriff verweigert"),
    attempts: int = 2,
    timeout: int = FETCH_TIMEOUT,
    ready_markers: tuple[str, ...] = (),
    prefer_http: bool = True,
) -> str:
    """
    Забирает HTML страницы.

    Сначала пробуем обычный HTTP-запрос — это полсекунды против десятков
    секунд у браузера. Если сайт ответил заглушкой или данных в ответе нет,
    поднимаем настоящий браузер в отдельном процессе (до `attempts` попыток).
    """
    import asyncio
    import multiprocessing
    import shutil
    import tempfile
    import time
    from pathlib import Path

    import os
    import signal

    if prefer_http:
        html = await fetch_html_fast(url, referer=warmup_url)
        if content_ok(html, ready_markers, blocked_markers):
            return html

    ctx = multiprocessing.get_context("spawn")
    base = _temp_base()
    _sweep_stale_temp(base)
    last_err: str = "не удалось загрузить страницу"

    def _kill_tree(p) -> None:
        """Прибиваем всю группу: иначе Chrome останется сиротой и займёт память."""
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            pass
        try:
            p.kill()
        except Exception:
            pass

    for attempt in range(attempts):
        # Каталогом владеет родитель: даже если дочерний процесс убит,
        # профиль Chrome (около 56 МБ) удалится вместе с ним
        tmp = Path(tempfile.mkdtemp(prefix="autokp_scrape_", dir=base))
        out, err = tmp / "page.html", tmp / "error.txt"
        profile = tmp / "chrome-profile"
        proc = ctx.Process(
            target=_browser_worker,
            args=(url, warmup_url, tuple(blocked_markers), str(out), str(err),
                  str(profile), tuple(ready_markers)),
            daemon=True,
        )
        try:
            proc.start()
            deadline = time.monotonic() + timeout
            while proc.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.4)

            if proc.is_alive():
                _kill_tree(proc)
                proc.join(5)
                last_err = "сайт не ответил вовремя"
            elif out.exists():
                return out.read_text(encoding="utf-8")
            elif err.exists():
                last_err = err.read_text(encoding="utf-8").strip() or last_err
            else:
                last_err = "браузер завершился, не отдав страницу"
        except Exception as e:
            last_err = str(e)
        finally:
            if proc.is_alive():
                _kill_tree(proc)
                proc.join(3)
            shutil.rmtree(tmp, ignore_errors=True)

        if attempt < attempts - 1:
            await asyncio.sleep(4)

    raise Exception(last_err)


# ── httpx-фолбэк (почти всегда блокируется, оставлен как страховка) ──────────

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,de;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


async def fetch_html_fast(url: str, referer: str = "") -> str:
    """
    Обычный HTTP-запрос без браузера — примерно полсекунды вместо десятков.
    Возвращает пустую строку, если не вышло: решение о пригодности
    принимает вызывающий по content_ok().
    """
    import httpx

    try:
        async with httpx.AsyncClient(headers=_HTTP_HEADERS, follow_redirects=True,
                                     timeout=20) as client:
            if referer:
                client.headers["Referer"] = referer
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""
            return resp.text
    except Exception:
        return ""


async def _scrape_with_httpx(url: str) -> dict:
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,de;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
        try:
            await client.get("https://www.mobile.de/ru/", timeout=10)
        except Exception:
            pass
        client.headers["Referer"] = "https://www.mobile.de/ru/"
        resp = await client.get(url)
        resp.raise_for_status()
        return _parse_html(resp.text)


# ── Публичные точки входа ────────────────────────────────────────────────────

# Куски, по которым видно, что страница объявления доехала целиком
MOBILE_DE_READY = ("self.__next_f", "classistatic.de")


async def scrape_mobile_de(url: str) -> dict:
    """Загружает и разбирает объявление mobile.de (ссылка любой локали)."""
    url = normalise_mobile_de(url)
    html = await fetch_html(url, ready_markers=MOBILE_DE_READY)
    data = _parse_html(html)

    # Цены нет — значит пришла не та страница; пробуем ещё раз браузером
    if not data.get("price_eur"):
        html = await fetch_html(url, ready_markers=MOBILE_DE_READY, prefer_http=False)
        data = _parse_html(html)
    return data


async def scrape(url: str) -> dict:
    """Единая точка входа: сам определяет площадку по ссылке."""
    source = detect_source(url)
    if source == SOURCE_AUTOSCOUT:
        from autoscout24 import scrape_autoscout24
        return await scrape_autoscout24(url)
    return await scrape_mobile_de(url)
