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

# русскоязычные ИГРОВЫЕ ленты (порядок = приоритет)
# 3DNews убран: это общий IT-портал, оттуда лезли новости про Windows и железо.
# VGTimes/DTF-игры/Kanobu проверены — RSS отдают 404 или битый XML, не берём.
FEEDS = (
    ("StopGame", "https://rss.stopgame.ru/rss_news.xml"),
    ("Playground", "https://www.playground.ru/rss/news.xml"),
    ("Igromania", "https://www.igromania.ru/rss/news.xml"),
    ("DTF", "https://dtf.ru/rss"),
)

MAX_IMG_BYTES = 8 * 1024 * 1024
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_WS_RE = re.compile(r"\s+")
_SENT_RE = re.compile(r"[.!?…]+(?=\s|$)")

# мусорные заголовки/анонсы, которые не нужны в канале
_SKIP_WORDS = ("скидк", "распродаж", "промокод", "конкурс", "розыгрыш", "гороскоп",
               "рецепт", "диета", "криптовалют", "курс рубл", "прогноз погод")

# Новости ДОЛЖНЫ быть про игры. Заголовки без игровых слов и с
# «околоигровым» мусором (кино, сериалы, Windows, железо, аккаунты) — мимо.
_SKIP_TOPICS = (
    "windows", "iphone", "android", "смартфон", "процессор", "видеокарт",
    "нейросет", "chatgpt", "openai", "nvidia geforce", "intel", "amd ryzen",
    "сериал", "сериал", "кинокаст", "кастинг", "актер", "актёр", "фильм",
    "netflix", "disney", "hbo", "марвел", "marvel", "перси джексон",
    "дженнифер", "звездные войны", "взлом", "украденные аккаунт", "мошенник",
    "вирус", "майнинг", "разыгрыва", "подарок", "смарт", "телефон",
    "приложение для", "браузер", "операционная систем",
    # «околоигровой» флейвор: трибьюты-рисунки, рекорды Гиннесса, акции
    # провайдеров и прочий инфошум вокруг игры, а не новость об игре.
    "провайдер", "интернет-трафик", "скорость подключения", "тариф",
    "нарисовать", "рисунок", "tattoo", "татуировк", "торт", "свадьб",
    "гиннесс", "рекорд гиннесса",
)

# Вес темы: чем важнее для игрока — тем выше. Крупные анонсы (GTA 6,
# Half-Life, Elden Ring...) должны попадать в канал раньше мелких.
# ВАЖНО: список намеренно широкий — новости не должны быть только про GTA.
# Интересны популярные игры и темы вообще: серии, студии, платформы, события.
_TOPIC_WEIGHT = (
    # --- флагманские тайтлы (максимальный интерес) ---
    ("gta", 60), ("grand theft auto", 60), ("gta 6", 80), ("gta vi", 80),
    ("half-life", 50), ("half life", 50), ("portal", 35), ("left 4 dead", 35),
    ("red dead", 45), ("rockstar", 45),
    ("stellar blade", 38), ("shift up", 28),
    ("elden ring", 45), ("dark souls", 35), ("fromsoftware", 40),
    ("duskbloods", 40), ("sekiro", 30), ("armored core", 30),
    ("cyberpunk", 42), ("witcher", 45), ("ведьмак", 45), ("cd projekt", 35),
    ("baldur", 38), ("larian", 30),
    ("tes", 25), ("elder scrolls", 40), ("skyrim", 35), ("fallout", 35),
    ("starfield", 30), ("bethesda", 35),
    ("zelda", 42), ("mario", 38), ("pokemon", 38), ("покемон", 35),
    ("metroid", 32), ("nintendo", 35), ("switch 2", 38), ("switch", 28),
    ("god of war", 38), ("last of us", 38), ("uncharted", 30),
    ("horizon", 30), ("ghost of", 28), ("naughty dog", 32),
    ("spider-man", 32), ("marvel's", 25), ("wolverine", 28),
    ("resident evil", 38), ("silent hill", 38), ("capcom", 32),
    ("monster hunter", 35), ("devil may cry", 30),
    ("final fantasy", 38), ("square enix", 32), ("kingdom hearts", 28),
    ("nier", 35), ("dragon quest", 30),
    ("metal gear", 35), ("konami", 28), ("death stranding", 35),
    ("kojima", 35),
    ("diablo", 35), ("world of warcraft", 32), ("overwatch", 28),
    ("blizzard", 32), ("starcraft", 28), ("warcraft", 30),
    ("assassin's creed", 35), ("far cry", 30), ("ubisoft", 32),
    ("rainbow six", 28), ("splinter cell", 28), ("prince of persia", 28),
    ("call of duty", 32), ("battlefield", 30), ("activision", 30),
    ("halo", 35), ("gears of war", 30), ("forza", 30), ("microsoft", 25),
    ("sony", 28), ("playstation", 32), ("xbox", 28),
    ("bloodborne", 35), ("tekken", 30), ("bandai", 28), ("soulcalibur", 25),
    ("persona", 32), ("atlus", 28), ("yakuza", 30), ("like a dragon", 28),
    ("sega", 28), ("sonic", 30),
    ("stalker", 35), ("metro", 30), ("atomic heart", 28),
    ("mass effect", 35), ("dragon age", 30), ("bioware", 32),
    ("assassin", 25), ("crimson desert", 32), ("pearl abyss", 28),
    ("hogwarts", 30), ("harry potter", 28),
    ("minecraft", 35), ("roblox", 28), ("fortnite", 30), ("epic games", 30),
    ("valorant", 28), ("league of legends", 30), ("riot games", 28),
    ("counter-strike", 35), ("cs2", 30), ("dota", 30),
    ("pubg", 28), ("apex legends", 28), ("ea sports", 25),
    ("deathloop", 25), ("dishonored", 28), ("arkane", 25),
    ("tomb raider", 30), ("thief", 25), ("deus ex", 28),
    ("total war", 28), ("civilization", 28), ("age of empires", 28),
    ("xcom", 25), ("warhammer", 28), ("40,000", 25),
    ("witcher 4", 50), ("масс эффект", 28), (" ведьмак", 30),
    # --- студии/издатели/сервисы (интересна новость, а не только игра) ---
    ("valve", 42), ("steam", 28), ("steam deck", 35), ("epic store", 28),
    ("gog", 25), ("take-two", 32), ("electronic arts", 28), ("ea games", 22),
    ("ubisoft", 32), ("thq", 25), ("deep silver", 22), ("focus", 20),
    ("paradox", 25), ("devolver", 22), ("annapurna", 22),
    # --- события и форматы (умеренный вес, не перебивают игры) ---
    ("распродаж", 30), ("sale", 20), ("steam awards", 35),
    ("game awards", 35), ("gamescom", 28), ("e3", 25), ("tga", 25),
    ("датамайн", 28), ("слив", 22), ("утечк", 22),
    ("ремастер", 25), ("ремейк", 28), ("порт", 18), ("pc-версия", 22),
    ("ранний доступ", 22), ("early access", 20), ("бесплатн", 20),
)

# Слова, которые сами по себе НЕ делают новость интересной: без имени игры
# или бренда они дают нулевой вес, иначе в канал лезет информационный шум
# (провайдер, пилот, аналитик...).
_CONTEXT_WORDS = (
    ("анонс", 12), ("релиз", 12), ("вышл", 12), ("выйдет", 12),
    ("трейлер", 12), ("обзор", 10), ("патч", 10), ("обновлени", 10),
    ("дополнени", 12), ("dlc", 12), ("демо", 10), ("продаж", 10),
    ("млн копий", 15), ("тираж", 12), ("отчёт", 10), ("интервью", 10),
)


def _match_weight(t: str) -> int:
    """Сумма весов за бренды/темы + слабый контекст.

    Контекстные слова (анонс/релиз/патч) добавляются только если в новости
    уже есть игра/бренд — иначе шум вроде «провайдер предложил...» ничего
    не получает и не обгоняет реальные игровые новости.
    """
    score = 0
    brand_hit = False
    for key, w in _TOPIC_WEIGHT:
        if key in t:
            score += w
            brand_hit = True
    if brand_hit:
        for key, w in _CONTEXT_WORDS:
            if key in t:
                score += w
    return score


def topic_score(title: str, summary: str = "") -> int:
    """Насколько новость интересна подписчику канала скидок (0..100+)."""
    t = f"{title} {summary}".lower()
    if any(w in t for w in _SKIP_TOPICS):
        return -1000  # не про игры — не берём вообще
    return _match_weight(t)


def is_game_news(title: str, summary: str = "") -> bool:
    """Новость про игры (а не про кино/железо/крипту)?"""
    return topic_score(title, summary) > -1000


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
    """Все статьи со всех лент: только игровые, важные — первыми."""
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
            # только игровая тематика: кино, железо, крипта и прочее — мимо
            a["topic"] = topic_score(a["title"], a.get("summary", ""))
            if a["topic"] <= -1000:
                continue
            seen.add(a["key"])
            items.append(a)
    # сначала самые важные темы (GTA 6, Valve, FromSoftware...), внутри — свежие
    items.sort(key=lambda a: (a.get("topic", 0), a["published"] or 0), reverse=True)
    return items


def pick_article(posted_check, max_age_hours: float = 48.0) -> dict | None:
    """Самая значимая свежая игровая новость, которой ещё не было в канале.

    Приоритет: важность темы (topic_score), затем наличие картинки и свежесть.
    Сильно устаревшие новости (старше max_age_hours) не берём — канал про
    актуальное, а не про «новость недельной давности».
    """
    items = fetch_all()
    if not items:
        return None
    now = time.time()
    fresh = []
    for a in items:
        if posted_check(a["key"]):
            continue
        ts = a.get("published") or 0
        # без даты считаем новость свежей (некоторые RSS её не отдают)
        if ts and (now - ts) / 3600 > max_age_hours:
            continue
        fresh.append(a)
    if not fresh:
        log.info("Нет свежих неопубликованных игровых новостей (%d в лентах)", len(items))
        return None
    with_img = [a for a in fresh if a.get("image")]
    pool = with_img or fresh
    best = pool[0]
    log.info("Новость выбрана: %s (важность %s)", best["title"][:60], best.get("topic"))
    return best


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
