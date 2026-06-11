import re
from bs4 import BeautifulSoup

_FEAT_RE = re.compile(r'\\"li\\",\\"([^\\"]{2,80})\\",\{\\"className\\":\\"CheckList')
_ATTR_RE = re.compile(r'\\"tag\\":\\"([^\\"]+)\\",\\"value\\":\\"([^\\"]+)\\"')


# ── RSC helpers ───────────────────────────────────────────────────────────────

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


def _uid_to_url(uid: str) -> str:
    return f"https://img.classistatic.de/api/v1/mo-prod/images/{uid[:2]}/{uid}?rule=mo-640.jpg"


def _extract_images(rsc: str) -> list[str]:
    """
    Extract car gallery images from RSC payload using cluster analysis.

    Strategy: all images in the RSC are found with their byte-positions.
    Car gallery images appear as a dense cluster (array of 10-50+ refs close
    together), while dealer logos / badge icons appear in isolation.
    We pick the largest cluster — that is the gallery.

    Returns deduplicated URLs for that cluster only.
    """
    uuid_re = re.compile(
        r"img\.classistatic\.de/api/v1/mo-prod/images/[a-f0-9]{2}/([a-f0-9\-]{36})"
    )
    matches = list(uuid_re.finditer(rsc))
    if not matches:
        return []

    # Group consecutive matches that are within GAP bytes of each other
    GAP = 800   # chars; images in the same JSON array are much closer than this
    groups: list[list[re.Match]] = []
    cur: list[re.Match] = [matches[0]]
    for m in matches[1:]:
        if m.start() - cur[-1].end() < GAP:
            cur.append(m)
        else:
            groups.append(cur)
            cur = [m]
    groups.append(cur)

    # Largest group = car gallery
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
    """Return ALL images in order of appearance — used by debug tool only."""
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
    return list(dict.fromkeys(_FEAT_RE.findall(rsc)))


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
    return {
        "photos":     _extract_images(rsc),          # gallery cluster only (smart)
        "all_photos": _extract_all_images_flat(rsc), # every image — for debug tool
        "price_eur": price_eur,
        "make": make,
        "model": model,
        "year": year,
        "mileage": mileage,
        "color": color,
        "features": _extract_features(rsc),
    }


# ── Playwright scraper (primary) ──────────────────────────────────────────────

async def _scrape_with_nodriver(url: str) -> dict:
    import asyncio
    import nodriver as uc

    import sys
    headless = sys.platform != "win32"
    browser = await uc.start(
        headless=headless,
        browser_args=["--lang=ru-RU", "--no-sandbox", "--disable-dev-shm-usage"],
    )
    try:
        # Warm up: homepage first so Cloudflare sets cookies + JS challenge resolves
        await browser.get("https://www.mobile.de/ru/")
        await asyncio.sleep(5)

        html = ""
        for attempt in range(3):
            page = await browser.get(url)
            wait = 6 + attempt * 4       # 6s → 10s → 14s
            await asyncio.sleep(wait)
            html = await page.get_content()
            if "Access denied" not in html and "Zugriff verweigert" not in html:
                break
            if attempt < 2:
                await asyncio.sleep(5)   # extra pause before next attempt
    finally:
        browser.stop()

    if "Access denied" in html or "Zugriff verweigert" in html:
        raise Exception("mobile.de отклонил запрос. Попробуй ещё раз через минуту.")

    return _parse_html(html)


# ── httpx fallback scraper ────────────────────────────────────────────────────

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


# ── Public entry point ────────────────────────────────────────────────────────

def _run_nodriver_in_thread(url: str) -> dict:
    """Run nodriver in a fresh thread with its own ProactorEventLoop (Windows needs this)."""
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    if hasattr(_asyncio, "WindowsProactorEventLoopPolicy"):
        _asyncio.set_event_loop_policy(_asyncio.WindowsProactorEventLoopPolicy())
    _asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_scrape_with_nodriver(url))
    finally:
        loop.close()


async def scrape_mobile_de(url: str) -> dict:
    """
    Run nodriver up to 3 times in isolated threads (each attempt = fresh browser).
    Cloudflare warms up after the first attempt so subsequent tries usually succeed.
    httpx is a last-resort fallback (almost always blocked, but kept as safety net).
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_event_loop()
    last_err: Exception | None = None

    for attempt in range(3):
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                return await loop.run_in_executor(pool, _run_nodriver_in_thread, url)
        except Exception as e:
            last_err = e
            if attempt < 2:
                # Brief pause — Cloudflare caches the IP from prior attempt
                await asyncio.sleep(4)

    # All nodriver attempts exhausted — try plain httpx as last resort
    try:
        return await _scrape_with_httpx(url)
    except Exception as e:
        raise Exception(f"{e}") from e
