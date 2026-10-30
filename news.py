"""Свежие игровые новости: RSS русскоязычных порталов + разбор в чистый текст.

Модуль независим от Telegram: отдаёт список статей, картинку и разметку ссылки.
Публикацией занимается poster.post_news(), дедупликация — storage.posted_news.
"""
import hashlib
import html as html_lib
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

from config import cfg

log = logging.getLogger("news")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# русскоязычные игровые ленты (порядок = приоритет)
FEEDS = (
    ("StopGame", "https://rss.stopgame.ru/rss_news.xml"),
    ("Playground", "https://www.playground.ru/rss/news.xml"),
    ("Igromania", "https://www.igromania.ru/rss/news.xml"),
    ("3DNews", "https://3dnews.ru/games/rss/"),
)

MAX_IMG_BYTES = 8 * 1024 * 1024
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_WS_RE = re.compile(r"\s+")
_SENT_RE = re.compile(r"[.!?…]+(?=\s|$)")

# мусорные заголовки/анонсы, которые не нужны в канале
_SKIP_WORDS = ("скидк", "распродаж", "промокод", "конкурс", "розыгрыш", "гороскоп")


def _clean(text: str) -> str:
    t = _TAG_RE.sub(" ", text or "")
    t = html_lib.unescape(t)
    return _WS_RE.sub(" ", t).strip()


def _clean_url(url: str) -> str:
    """Убирает utm-метки и якорь: ссылка в посте должна быть короткой и чистой."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    url = html_lib.unescape((url or "").strip())
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), ""))


def _latin_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) < 128) / len(letters)


def _first_sentences(text: str, max_chars: int = 600, limit: int = 3) -> str:
    parts: list[str] = []
    total = 0
    pos = 0
    for m in _SENT_RE.finditer(text):
        sent = text[pos:m.end()].strip()
        pos = m.end()
        if not sent:
            continue
        if total + len(sent) + (1 if parts else 0) > max_chars:
            break
        parts.append(sent)
        total += len(sent) + 1
        if len(parts) >= limit:
            break
    out = " ".join(parts).strip()
    if not out and text:
        out = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return out


def _fetch(url: str, timeout: int = 20) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            log.warning("RSS %s -> HTTP %s", url, r.status_code)
            return ""
        # RSS приходит в utf-8 или windows-1251
        return r.content.decode("utf-8", errors="replace")
    except requests.RequestException as e:
        log.warning("RSS %s: %s", url, cfg.mask(e))
        return ""


def _local(tag: str) -> str:
    return tag.split("}")[-1].lower()


def _parse_rss(xml_text: str, source: str) -> list[dict]:
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as e:
        log.warning("%s: битый RSS (%s)", source, e)
        return []

    out: list[dict] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        fields: dict[str, str] = {}
        media: list[str] = []
        for child in item:
            name = _local(child.tag)
            if name in ("title", "link", "guid", "description", "pubdate", "summary"):
                fields.setdefault(name, (child.text or "").strip())
            if name in ("enclosure", "content", "thumbnail"):
                u = child.attrib.get("url") or child.attrib.get("{http://www.w3.org/2005/Atom}href")
                if u:
                    media.append(u)
            if name == "content" and (child.text or "").strip():
                fields.setdefault("description", (child.text or "").strip())

        title = _clean(fields.get("title", ""))
        url = _clean_url(fields.get("link") or fields.get("guid") or "")
        if not title or not url.startswith("http"):
            continue
        if any(w in title.lower() for w in _SKIP_WORDS):
            continue

        raw_desc = fields.get("description", "")
        img = _IMG_RE.search(raw_desc)
        if img:
            media.append(img.group(1))

        ts = 0
        try:
            dt = parsedate_to_datetime(fields.get("pubdate", ""))
            ts = int(dt.timestamp())
        except (TypeError, ValueError):
            ts = 0

        out.append({
            "title": title,
            "url": url,
            "summary": _first_sentences(_clean(raw_desc)),
            "source": source,
            "published": ts,
            "image": (media[0] if media else "").strip(),
            "key": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
        })
    return out


def fetch_all() -> list[dict]:
    """Все статьи со всех лент (свежие первыми)."""
    items: list[dict] = []
    seen: set[str] = set()
    for source, url in FEEDS:
        for a in _parse_rss(_fetch(url), source):
            if a["key"] in seen:
                continue
            if _latin_ratio(a["title"]) > 0.6:
                continue  # не русскоязычная новость
            if len(a["title"]) < 15:
                continue
            seen.add(a["key"])
            items.append(a)
    items.sort(key=lambda a: a["published"] or 0, reverse=True)
    return items


def pick_article(posted_check) -> dict | None:
    """Первая свежая новость, которой ещё не было в канале. Ищем с картинкой."""
    items = fetch_all()
    if not items:
        return None
    fresh = [a for a in items if not posted_check(a["key"])]
    if not fresh:
        log.info("Все новости уже публиковались (%d в лентах)", len(items))
        return None
    with_img = [a for a in fresh if a.get("image")]
    return (with_img or fresh)[0]


def download_image(url: str, path: str) -> bool:
    """Качает картинку новости (не только Steam-домены: нужен любой хост RSS)."""
    if not url.lower().startswith("https://"):
        return False
    try:
        with requests.get(url, headers=HEADERS, timeout=30, stream=True) as r:
            if r.status_code != 200:
                return False
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                return False
            total = 0
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > MAX_IMG_BYTES:
                        f.close()
                        os.remove(path)
                        return False
                    f.write(chunk)
        ok = os.path.getsize(path) > 5000
        if not ok:
            os.remove(path)
        return ok
    except (requests.RequestException, OSError) as e:
        log.warning("картинка новости: %s", cfg.mask(e))
        return False


def image_path(article: dict) -> str:
    """Скачивает обложку новости в images/news_<key>.jpg (или '')."""
    out = os.path.join(cfg.img_dir, f"news_{article['key']}.jpg")
    if os.path.exists(out) and os.path.getsize(out) > 5000:
        return out
    if not article.get("image"):
        return ""
    return out if download_image(article["image"], out) else ""


def newest_age_hours(items: list[dict]) -> float:
    if not items:
        return 999.0
    ts = max((a["published"] or 0) for a in items)
    return max(0.0, (time.time() - ts) / 3600) if ts else 999.0
