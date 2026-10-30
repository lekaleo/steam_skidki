"""Клиент Steam Store API: все скидки + детали игры по регионам + RU-конвертация цен.

Возможности:
- парсинг рейтинга отзывов прямо из поиска (data-tooltip-html у search_review_summary);
- детали игры по 4 регионам (ru / kz / ua / us) — цены во ВСЕХ валютах;
- русская локализация из supported_languages (<strong>*</strong> = полная озвучка);
- рекомендации Steam и Metacritic для отсева «треша»;
- кэш деталей в памяти (чтобы не долбить Steam при подборе ending/серий).
"""
import html as html_lib
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

import currency
from config import cfg
from storage import Game

log = logging.getLogger("steam")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

SEARCH_URL = "https://store.steampowered.com/search/results/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

# разрешённые домены для картинок (анти-DoS / SSRF)
ALLOWED_IMG_HOSTS = (
    "shared.akamai.steamstatic.com",
    "cdn.akamai.steamstatic.com",
    "shared.fastly.steamstatic.com",
    "cdn.fastly.steamstatic.com",
    "store.steampowered.com",
    "steamcdn-a.akamaihd.net",
    "akamai.steamstatic.com",
)
MAX_IMG_BYTES = 10 * 1024 * 1024

SKIP_TYPES = {"demo", "music", "video", "series", "mod", "advertising", "hardware", "config"}

# кэши в памяти процесса
_st_cache: dict = {}
_details_cache: dict[int, tuple[float, dict]] = {}
DETAILS_TTL = 1800  # 30 минут

_TITLE_RE = re.compile(r'<span class="title">(.*?)</span>', re.S)
_DISC_RE = re.compile(r'data-discount="(\d+)"')
_FINAL_RE = re.compile(r'discount_final_price[^>]*>([^<]+)<')
_ORIG_RE = re.compile(r'discount_original_price[^>]*>([^<]+)<')
_IMG_RE = re.compile(r'<img src="([^"]+)"')

APP_FILTERS = "basic,price_overview,genres,supported_languages,recommendations,metacritic,release_date"


# ============================================================ SEARCH

def search_specials(max_pages: int | None = None, min_discount: int | None = None) -> list[Game]:
    """Все распродажные игры через search?specials=1 (JSON + HTML rows)."""
    max_pages = max_pages if max_pages is not None else cfg.max_search_pages
    min_discount = min_discount if min_discount is not None else cfg.min_discount
    games: dict[int, Game] = {}
    page = 0
    while True:
        if max_pages and page >= max_pages:
            break
        params = {
            "query": "", "start": page * 100, "count": 100, "specials": 1,
            "cc": "ru", "l": "russian", "format": "json", "infinite": 1,
        }
        text = _get_text(SEARCH_URL, params)
        if not text:
            break
        try:
            data = _json_loads(text)
            html = (data or {}).get("results_html") or (data or {}).get("items_html") or ""
        except ValueError:
            html = text
        if not html or not html.strip():
            break
        pieces = re.split(r'data-ds-appid="(\d+)"', html)
        rows = 0    # всего игр на странице (до фильтра скидки)
        found = 0   # прошло фильтр min_discount
        for i in range(1, len(pieces) - 1, 2):
            appid = int(pieces[i])
            chunk = pieces[i + 1]
            title_m = _TITLE_RE.search(chunk)
            if not title_m:
                continue
            rows += 1
            title = _strip_tags(title_m.group(1))
            disc_m = _DISC_RE.search(chunk)
            discount = int(disc_m.group(1)) if disc_m else 0
            if discount < min_discount:
                continue
            img_m = _IMG_RE.search(chunk)
            img_url = img_m.group(1) if img_m else ""
            fin_m = _FINAL_RE.search(chunk)
            orig_m = _ORIG_RE.search(chunk)
            pct, cnt, label = _parse_reviews(chunk)
            games[appid] = Game(
                appid=appid, title=title, discount=discount,
                final=_parse_money(fin_m.group(1)) if fin_m else None,
                initial=_parse_money(orig_m.group(1)) if orig_m else None,
                currency="RUB", img_url=img_url,
                review_pct=pct, review_count=cnt, review_label=label,
                score=compute_score(pct, cnt, discount),
            )
            found += 1
        page += 1
        # «последняя страница» определяем по сырому числу строк, а не по found:
        # фильтр скидки режет часть игр, и раньше это обрывало обход уже на 1-й странице
        if rows < 100:
            break
        time.sleep(cfg.steam_delay)
    return list(games.values())


def _parse_reviews(chunk: str) -> tuple[int, int, str]:
    """Достаёт % положительных отзывов, их количество и словесный рейтинг."""
    i = chunk.find("search_review_summary")
    if i < 0:
        return 0, 0, ""
    win = chunk[i:i + 600]
    m = re.search(r'data-tooltip-html="([^"]*)"', win)
    if not m:
        return 0, 0, ""
    tip = html_lib.unescape(m.group(1))
    label = re.split(r"<br\s*/?>", tip)[0].strip()
    pct_m = re.search(r"(\d{1,3})\s*%", tip)
    pct = int(pct_m.group(1)) if pct_m else 0
    # формат Steam: «97% из 1,480 обзоров» / «96% of the 123,456 user reviews»
    count = 0
    count_m = re.search(r"%[^0-9]{0,25}?([0-9][0-9\s,\.]{2,})", tip)
    if count_m:
        digits = re.sub(r"\D", "", count_m.group(1))
        if digits:
            count = int(digits)
    return pct, count, label


def compute_score(review_pct: int = 0, review_count: int = 0, discount: int = 0,
                  metacritic: int = 0) -> int:
    """Комбинированный вес игры: качество + популярность + размер скидки.

    Известная игра с 95% отзывов всегда обгонит ноунейм-треш с 90% скидки
    и нулём отзывов.
    """
    score = 0
    score += int(review_pct or 0) * 6              # до 600
    score += min(int(review_count or 0), 500000) // 2000  # до 250
    score += int(discount or 0) * 2                # до 200
    if metacritic:
        score += int(metacritic) * 2               # до 200
    return score


def _parse_money(s: str) -> float | None:
    if not s or "NA" in s.upper() or "бесплатно" in s.lower():
        return None
    digits = re.sub(r"[^\d.,]", "", s)
    if not digits:
        return None
    try:
        return float(digits.replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ============================================================ APP DETAILS

def appdetails(appid: int, cc: str = "ru") -> dict | None:
    """Детали игры в одной валюте. Всегда l=russian (описание, жанры, языки)."""
    data = _get_json(
        APPDETAILS_URL,
        {"appids": appid, "cc": cc, "l": "russian", "filters": APP_FILTERS},
        timeout=20,
    )
    if not data:
        return None
    entry = data.get(str(appid)) or data.get(appid)
    if not entry or not entry.get("success"):
        return None
    return entry.get("data") or None


def fetch_game_details(appid: int, force: bool = False) -> dict:
    """Описание, цены ₽/₸/₴/$ , картинка, дата конца, локализация, рейтинг."""
    now = time.time()
    hit = _details_cache.get(appid)
    if hit and not force and now - hit[0] < DETAILS_TTL:
        return hit[1]

    ru = appdetails(appid, "ru")
    time.sleep(cfg.steam_delay)
    kz = appdetails(appid, "kz")
    time.sleep(cfg.steam_delay)
    ua = appdetails(appid, "ua")
    time.sleep(cfg.steam_delay)
    us = appdetails(appid, "us")
    time.sleep(cfg.steam_delay)

    price_ru = _price_of(ru)
    price_kz = _price_of(kz)
    price_ua = _price_of(ua)
    price_us = _price_of(us)

    # если в РФ недоступно — считаем рублёвую цену по курсу (KZ -> UA -> US)
    if not price_ru:
        price_ru = _convert_to_rub(price_kz, price_ua, price_us)

    base = ru or kz or ua or us or {}
    raw_desc = (base.get("short_description") or "").strip()
    # Описание обязано быть на русском: если Steam отдал английский текст
    # (у игры нет русской локализации) — переводим через translate_ru().
    desc_ru = _clean_html(raw_desc)
    translated = False
    if desc_ru and _latin_ratio(desc_ru) > 0.35:
        tr = _clean_html(translate_ru(desc_ru))
        if tr and len(tr) >= 20 and _latin_ratio(tr) < 0.5:
            desc_ru, translated = tr, True
    name = base.get("name", str(appid))
    genres = [g.get("description", "") for g in (base.get("genres", []) or []) if g.get("description")]

    recs = base.get("recommendations") or {}
    mc = base.get("metacritic") or {}
    rd = base.get("release_date") or {}
    release_raw = (rd.get("date") or "").strip()
    out = {
        "name": name,
        "type": base.get("type", "game"),
        # short_desc — уже переведённое и сжатое (для карточек/фолбэков),
        # short_desc_raw — русский текст как есть: из него берём 1–2 предложения.
        "short_desc": process_description(desc_ru, name, genres),
        "short_desc_raw": desc_ru,
        "desc_translated": translated,
        "header_image": base.get("header_image", ""),
        "genres": genres,
        "price_ru": price_ru,
        "price_kz": price_kz,
        "price_ua": price_ua,
        "price_us": price_us,
        "exp_date": _exp_of(ru) or _exp_of(kz) or _exp_of(ua) or _exp_of(us),
        "store_url": f"https://store.steampowered.com/app/{appid}/",
        "ru_lang": _ru_lang(base),
        "reviews_total": int(recs.get("total") or 0),
        "metacritic": int(mc.get("score") or 0),
        "release_date": release_raw,
        "coming_soon": bool(rd.get("coming_soon")),
        "release_ts": parse_release_ts(release_raw),
        "is_free": bool(base.get("is_free")),
        "developers": [d for d in (base.get("developers") or []) if d],
    }
    _details_cache[appid] = (time.time(), out)
    return out


def _ru_lang(d: dict) -> str:
    """Статус русской локализации по supported_languages.

    Steam помечает полную озвучку звёздочкой: «Русский<strong>*</strong>».
    """
    langs = d.get("supported_languages") or ""
    if not langs:
        return "Языки не указаны 🌐"
    for part in re.split(r"<br\s*/?>|,", langs):
        if "усск" in part or "ussian" in part:
            if "<strong>" in part or "*" in part:
                return "Полная русская озвучка 🎙"
            return "Русские субтитры 💬"
    return "Без перевода 🌐"


# ============================================================ НОВИНКИ И РЕЛИЗЫ

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONTH_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _month_num(word: str) -> int:
    return _MONTHS.get(re.sub(r"[^а-яёa-z]", "", (word or "").lower())[:3], 0)


def parse_release_ts(text: str) -> int | None:
    """«15 ноя. 2026» / «Ноябрь 2026» / «2027» -> unix ts. None если даты нет."""
    s = (text or "").lower().replace("г.", " ").strip()
    if not s:
        return None
    m = re.search(r"(\d{1,2})\s+([а-яёa-z]+)\.?\s*,?\s*(\d{4})", s)
    if m:
        day, mon, year = int(m.group(1)), _month_num(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"([а-яёa-z]+)\.?\s*,?\s*(\d{4})", s)
        if m:
            day, mon, year = 1, _month_num(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"\b(19|20)\d{2}\b", s)
            if not m:
                return None
            day, mon, year = 1, 1, int(m.group(0))
    if not 1 <= mon <= 12:
        return None
    try:
        return int(datetime(year, mon, day, 12, tzinfo=cfg.tz).timestamp())
    except ValueError:
        return None


def format_release(ts: int | None, raw: str = "") -> str:
    """Дата релиза по-русски: «15 ноября 2026»."""
    if not ts:
        return (raw or "").strip()
    d = datetime.fromtimestamp(ts, cfg.tz)
    return f"{d.day} {_MONTH_RU[d.month - 1]} {d.year}"


FEATURED_URL = "https://store.steampowered.com/api/featuredcategories"
_RELEASE_RE = re.compile(r'search_released[^>]*>([^<]*)<', re.S)


def search_upcoming_and_new(limit: int = 20) -> dict[str, list[dict]]:
    """Скорые релизы и свежие новинки Steam.

    Основной источник — featuredcategories (coming_soon / new_releases),
    фолбэк — поиск Steam с фильтрами comingsoon / popularnew.

    Возвращает {"coming_soon": [...], "new_releases": [...]}, элементы:
    {"appid", "title", "release", "coming_soon"}.
    """
    out: dict[str, list[dict]] = {"coming_soon": [], "new_releases": []}
    data = _get_json(FEATURED_URL, {"cc": "ru", "l": "russian"}, timeout=25) or {}
    for section in ("coming_soon", "new_releases"):
        block = data.get(section) or {}
        seen: set[int] = set()
        for it in (block.get("items") or []):
            appid = it.get("id")
            if not appid or appid in seen:
                continue
            seen.add(int(appid))
            out[section].append({
                "appid": int(appid),
                "title": (it.get("name") or "").strip(),
                "release": "",
                "coming_soon": section == "coming_soon",
            })
            if len(out[section]) >= limit:
                break

    # фолбэк: если раздел пуст — обычный поиск Steam
    if not out["coming_soon"]:
        out["coming_soon"] = _search_rows("comingsoon", limit, coming_soon=True)
    if not out["new_releases"]:
        out["new_releases"] = _search_rows("popularnew", limit, coming_soon=False)
    return out


def _search_rows(filter_name: str, count: int, coming_soon: bool) -> list[dict]:
    """Фолбэк через search/results с фильтром comingsoon / popularnew."""
    params = {
        "query": "", "start": 0, "count": count, "filter": filter_name,
        "cc": "ru", "l": "russian", "format": "json", "infinite": 1,
    }
    text = _get_text(SEARCH_URL, params)
    if not text:
        return []
    try:
        html = (_json_loads(text) or {}).get("results_html") or ""
    except ValueError:
        html = text
    rows: list[dict] = []
    pieces = re.split(r'data-ds-appid="(\d+)"', html)
    for i in range(1, len(pieces) - 1, 2):
        appid = int(pieces[i])
        chunk = pieces[i + 1]
        title_m = _TITLE_RE.search(chunk)
        if not title_m:
            continue
        rel_m = _RELEASE_RE.search(chunk)
        rows.append({
            "appid": appid,
            "title": _strip_tags(title_m.group(1)),
            "release": _strip_tags(rel_m.group(1)) if rel_m else "",
            "coming_soon": coming_soon,
        })
        if len(rows) >= count:
            break
    return rows


def _price_of(d: dict | None) -> dict | None:
    if not d:
        return None
    p = d.get("price_overview") or {}
    # final может быть 0 (халява со скидкой 100%) — это валидная цена!
    if p.get("final") is None:
        return None
    return {
        "final": p["final"] / 100.0,
        "initial": (p.get("initial") or p["final"]) / 100.0,
        "discount": p.get("discount_percent", 0),
        "currency": p.get("currency", ""),
        "formatted": (p.get("final_formatted") or "").replace(",00", "").strip(),
    }


def _convert_to_rub(*sources) -> dict | None:
    """Берёт первую доступную цену и пересчитывает в RUB (флаг converted)."""
    for pr in sources:
        if pr and pr.get("final") is not None:
            cur = (pr.get("currency") or "").upper()
            final_rub = currency.to_rub(pr["final"], cur)
            initial = pr.get("initial")
            initial_rub = currency.to_rub(initial, cur) if initial else None
            return {
                "final": round(final_rub) if final_rub is not None else None,
                "initial": round(initial_rub) if initial_rub else None,
                "discount": pr.get("discount", 0),
                "currency": "RUB",
                "converted": True,
                "converted_from": cur,
                "source_final": pr["final"],
                "source_currency": cur,
            }
    return None


def _exp_of(d: dict | None) -> int | None:
    if not d:
        return None
    p = d.get("price_overview") or {}
    exp = p.get("discount_expiration_date")
    return exp if isinstance(exp, int) and exp else None


# ============================================================ DESCRIPTIONS (RU + сжатие)

def _latin_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for c in letters if ord(c) < 128)
    return latin / len(letters)


def translate_ru(text: str) -> str:
    """Бесплатный Google Translate endpoint с ретраями и кэшем. При ошибке — исходник."""
    if not text:
        return text
    key = "tr:" + str(hash(text))
    cached = _st_cache.get(key)
    if cached:
        return cached
    for attempt in range(3):
        try:
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
            )
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            data = r.json()
            parts = [seg[0] for seg in data[0] if seg and seg[0]]
            out = "".join(parts).strip() or text
            _st_cache[key] = out
            return out
        except (requests.RequestException, ValueError, IndexError, TypeError) as e:
            if attempt == 2:
                log.warning("translate_ru fail: %s", cfg.mask(e))
            else:
                time.sleep(1.5 + attempt * 2)
    return text


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_DANGLE = {"в", "на", "с", "со", "к", "ко", "о", "об", "обо", "по", "из", "от", "до",
           "для", "и", "а", "но", "или", "что", "как", "при", "без", "под", "над",
           "за", "у", "же", "ли", "бы", "это", "the", "of", "a", "an", "to", "in", "and"}


def compress(text: str, max_chars: int = 165) -> str:
    """Сжимает до 1–2 коротких предложений, обрезка по границе предложения/слова.

    Не оставляет «висячих» предлогов/союзов в конце («…франшиз в…»).
    """
    text = _clean_html(text)
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars + 40]
    m = None
    for m2 in re.finditer(r"[.!?…](\s|$)", cut):
        if m2.end() <= max_chars:
            m = m2
    if m:
        return cut[:m.end()].strip()
    piece = text[:max_chars]
    sp = piece.rfind(" ")
    if sp > 40:
        piece = piece[:sp]
    words = piece.split()
    while words and words[-1].lower().strip(",.!?…;:-—") in _DANGLE:
        words.pop()
    piece = " ".join(words)
    return piece.rstrip(" ,;:-—") + "…"


def process_description(raw: str, name: str = "", genres: list | None = None) -> str:
    """Чистое сжатое русское описание. Если английский (>40% латиницы) — перевести."""
    text = _clean_html(raw)
    if not text:
        return ""
    if _latin_ratio(text) > 0.40:
        text = _clean_html(translate_ru(text))
    return compress(text, max_chars=165)


# ============================================================ DOWNLOAD (hardened)

def _is_allowed_image(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme != "https":
        return False
    host = (u.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_IMG_HOSTS)


def download_image(url: str, path: str) -> bool:
    """Безопасное скачивание: только https Steam-домены, лимит 10 МБ, поток чанками."""
    if not url or not _is_allowed_image(url):
        log.warning("download_image: недопустимый URL %s", url[:120])
        return False
    try:
        with requests.get(url, headers=HEADERS, timeout=30, stream=True) as r:
            if r.status_code != 200:
                return False
            cl = r.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_IMG_BYTES:
                        log.warning("download_image: слишком большой файл (%s байт)", cl)
                        return False
                except ValueError:
                    pass
            total = 0
            chunks = []
            for chunk in r.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_IMG_BYTES:
                    log.warning("download_image: превышен лимит 10 МБ")
                    return False
                chunks.append(chunk)
            if total < 1000:
                return False
            with open(path, "wb") as f:
                f.write(b"".join(chunks))
            return True
    except requests.RequestException as e:
        log.warning("download_image error: %s", cfg.mask(e))
        return False


# ============================================================ low-level

def _get_json(url: str, params: dict, timeout: float = 25) -> dict | None:
    text = _get_text(url, params, timeout)
    if text is None:
        return None
    try:
        return _json_loads(text)
    except ValueError:
        return None


def _json_loads(text: str) -> dict:
    import json
    return json.loads(text)


def _get_text(url: str, params: dict, timeout: float = 25) -> str | None:
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                time.sleep(8 + attempt * 5)
                continue
            r.raise_for_status()
            return r.content.decode("utf-8", errors="replace")
        except requests.RequestException as e:
            if attempt == 3:
                log.warning("steam request failed: %s", cfg.mask(e))
                return None
            time.sleep(3 + attempt * 3)
    return None
