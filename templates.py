"""Текст постов Telegram: строгий премиальный макет без «колхоза» и воды.

Принципы:
- Никакой бухгалтерии: нет «Стоимость по регионам», «Выгода», «по курсу ₸».
  Сконвертированная цена — просто тильда: ~1 490 ₽.
- Описание — ОДНО законченное русское предложение (<= 130 символов).
  Нет чистого предложения или текст английский — абзац не выводим вообще.
- Все валюты всегда: 🇷🇺 ₽ · 🇰🇿 ₸ · 🇺🇦 ₴ · 🇺🇸 $ (эмодзи — из слотов).
- Эмодзи слотов хранятся в SQLite (kv) и настраиваются в личке бота:
  /emojis, /setemoji <слот> <эмодзи>, /resetemojis.
- Весь Steam/пользовательский текст экранируется через html.escape.
- Групповые посты сами укладываются в лимит подписи Telegram (1024 симв.).
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import time
from datetime import datetime

from config import cfg
from storage import Storage

CUR_SYM = {"RUB": "₽", "KZT": "₸", "UAH": "₴", "USD": "$", "EUR": "€"}


# ================================================================ СЛОТЫ ЭМОДЗИ
#
# Каждый слот = (символ по умолчанию, custom_emoji_id по умолчанию).
# Пользователь переопределяет любой слот командой /setemoji в личке бота —
# значение живёт в таблице kv (state.db) и подставляется во все новые посты.

EMOJI_SLOTS: dict[str, tuple[str, str]] = {
    "fire": ("🔥", "5368324170671202286"),
    "tag": ("🏷", "5384476616273760608"),
    "gem": ("💎", "5312411081962852416"),
    "gift": ("🎁", "5461151367559141950"),
    "star": ("⭐", "5334544901428229844"),
    "clock": ("⏳", "5384484358531581850"),
    "rub": ("🇷🇺", ""),
    "kzt": ("🇰🇿", ""),
    "uah": ("🇺🇦", ""),
    "usd": ("🇺🇸", ""),
    "footer": ("🔵", ""),
    "halloween": ("🎃", ""),
    "newyear": ("🎄", ""),
    "news": ("📰", ""),
}

SLOT_HINTS = {
    "fire": "главный огонёк в заголовке",
    "tag": "перед названием игры в подборке",
    "gem": "хиты и дешёвые игры",
    "gift": "халява",
    "star": "отзывы",
    "clock": "перед датой окончания",
    "rub": "перед ценой в рублях",
    "kzt": "перед ценой в тенге",
    "uah": "перед ценой в гривне",
    "usd": "перед ценой в долларах",
    "footer": "перед подписью канала",
    "halloween": "Хэллоуин",
    "newyear": "Новый год / зимняя распродажа",
    "news": "новинки и релизы",
}

_store: Storage | None = None
_emoji_cache: dict[str, tuple[float, dict]] = {}
_EMOJI_TTL = 60.0


def _storage() -> Storage:
    global _store
    if _store is None:
        _store = Storage(cfg.db_path)
    return _store


def _default_slot(slot: str) -> dict:
    char, eid = EMOJI_SLOTS.get(slot, ("", ""))
    return {"char": char, "id": eid}


def get_emoji(slot: str) -> dict:
    """Текущее значение слота: {"char": "🔥", "id": "5368…"} (кэш 60 сек)."""
    now = time.time()
    hit = _emoji_cache.get(slot)
    if hit and now - hit[0] < _EMOJI_TTL:
        return hit[1]
    val = _default_slot(slot)
    try:
        raw = _storage().kv_get(f"emoji:{slot}")
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("char"):
                val = {"char": str(data["char"]), "id": str(data.get("id") or "")}
    except (sqlite3.Error, ValueError, OSError):
        pass
    _emoji_cache[slot] = (now, val)
    return val


def emo(slot: str) -> str:
    """Эмодзи слота в HTML: <tg-emoji>, если есть id и включён премиум."""
    v = get_emoji(slot)
    char, eid = v["char"], v["id"]
    if not char:
        return ""
    if cfg.premium_emoji and eid:
        return f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'
    return char


def pe(name: str) -> str:
    """Совместимость со старым API: pe("fire") == emo("fire")."""
    return emo(name)


def set_emoji(slot: str, char: str, eid: str = "") -> bool:
    """Запоминает эмодзи слота навсегда (SQLite kv)."""
    slot = (slot or "").strip().lower()
    if slot not in EMOJI_SLOTS:
        return False
    char = (char or "").strip()
    if not char:
        return False
    _storage().kv_set(
        f"emoji:{slot}",
        json.dumps({"char": char, "id": (eid or "").strip()}, ensure_ascii=False),
    )
    _emoji_cache.pop(slot, None)
    return True


def reset_emojis() -> int:
    """Возвращает стильный набор по умолчанию. Сколько слотов сброшено."""
    n = 0
    for slot in EMOJI_SLOTS:
        try:
            _storage().kv_del(f"emoji:{slot}")
            n += 1
        except (sqlite3.Error, OSError):
            pass
    _emoji_cache.clear()
    return n


def emoji_table() -> list[tuple[str, dict, dict]]:
    """[(слот, текущее значение, значение по умолчанию)] — для /emojis."""
    return [(slot, get_emoji(slot), _default_slot(slot)) for slot in EMOJI_SLOTS]


def is_custom(slot: str) -> bool:
    """Слот переопределён пользователем?"""
    cur, dflt = get_emoji(slot), _default_slot(slot)
    return (cur["char"], cur["id"]) != (dflt["char"], dflt["id"])


# ================================================================ БАЗОВЫЕ УТИЛИТЫ

def _e(s) -> str:
    return html.escape(str(s), quote=False)


def ulen(s: str) -> int:
    """Длина строки так, как её считает Telegram (UTF-16 code units)."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


_TAG_ANY_RE = re.compile(r"<[^>]+>")


def vlen(s: str) -> int:
    """Длина ВИДИМОГО текста: Telegram не считает теги и URL ссылок."""
    return ulen(html.unescape(_TAG_ANY_RE.sub("", s)))


_TAG_RE = re.compile(r"</?(blockquote|b|i|s|code|a)\b[^>]*>", re.I)


def html_balanced(text: str) -> bool:
    """True, если все открытые теги закрыты (иначе Telegram вернёт ошибку)."""
    depth: dict[str, int] = {}
    for m in _TAG_RE.finditer(text):
        tag = m.group(1).lower()
        if m.group(0).startswith("</"):
            depth[tag] = depth.get(tag, 0) - 1
            if depth[tag] < 0:
                return False
        else:
            depth[tag] = depth.get(tag, 0) + 1
    return all(v == 0 for v in depth.values())


def _clean(text) -> str:
    """Убирает HTML Steam и лишние пробелы."""
    t = re.sub(r"<[^>]+>", " ", str(text or ""))
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _latin_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) < 128) / len(letters)


def _num(n) -> str:
    if n is None:
        return ""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    return f"{round(n):,}".replace(",", " ")


def _usd(n) -> str:
    try:
        return f"{float(n):.2f}"
    except (TypeError, ValueError):
        return ""


# ================================================================ ОПИСАНИЕ

MAX_SENTENCE = 130
_SENT_END_RE = re.compile(r"[.!?…]+(?=\s|$)")
_BAD_DESC = ("все права защищены", "©", "all rights reserved", "steam", "valve")


def one_sentence(details: dict) -> str:
    """ОДНО законченное русское предложение описания (или пусто).

    Правила: только русский текст (никакого машинного перевода), только
    первое законченное предложение, не длиннее 130 символов. Иначе — пусто,
    и в посте останется только строка жанров и локализации.
    """
    text = _clean(details.get("short_desc_raw") or "")
    if len(text) < 25:
        return ""
    if _latin_ratio(text) > 0.35:
        return ""
    m = _SENT_END_RE.search(text)
    if not m:
        return ""
    sent = text[: m.end()].strip()
    if len(sent) < 20 or len(sent) > MAX_SENTENCE:
        return ""
    low = sent.lower()
    if any(b in low for b in _BAD_DESC):
        return ""
    return _e(sent)


# ================================================================ ЦЕНЫ

def _region_line(slot: str, sym: str, pr: dict | None, usd: bool = False) -> str:
    """Одна строка цены: 🇷🇺 <b>849 ₽</b>  <s>1 699 ₽</s>."""
    if not pr or pr.get("final") is None:
        return ""
    final = float(pr["final"])
    if final <= 0:
        return f'{emo(slot)} <b>БЕСПЛАТНО</b>'
    tilde = "~" if pr.get("converted") else ""
    if usd:
        new = f"{tilde}${_usd(final)}"
        old = f"${_usd(pr.get('initial'))}" if _has_old(pr) else ""
    else:
        new = f"{tilde}{_num(final)} {sym}"
        old = f"{_num(pr.get('initial'))} {sym}" if _has_old(pr) else ""
    out = f"{emo(slot)} <b>{new}</b>"
    if old:
        out += f"  <s>{old}</s>"
    return out


def _has_old(pr: dict) -> bool:
    initial, final = pr.get("initial"), pr.get("final")
    try:
        return initial is not None and float(initial) > float(final)
    except (TypeError, ValueError):
        return False


def price_lines(details: dict) -> list[str]:
    """Четыре строки цен — без единого лишнего слова."""
    rows = [
        _region_line("rub", "₽", details.get("price_ru")),
        _region_line("kzt", "₸", details.get("price_kz")),
        _region_line("uah", "₴", details.get("price_ua")),
        _region_line("usd", "$", details.get("price_us"), usd=True),
    ]
    return [r for r in rows if r]


def _price_inline(d: dict) -> str:
    """Компактная строка валют для подборок: 🇷🇺 55 ₽ · 🇰🇿 410 ₸ · …"""
    parts = []
    rub = d.get("price_ru")
    if rub and rub.get("final") is not None:
        if float(rub["final"]) <= 0:
            parts.append(f'{emo("rub")} <b>БЕСПЛАТНО</b>')
        else:
            tilde = "~" if rub.get("converted") else ""
            parts.append(f'{emo("rub")} <b>{tilde}{_num(rub["final"])} ₽</b>')
    for slot, sym, key in (("kzt", "₸", "price_kz"), ("uah", "₴", "price_ua")):
        pr = d.get(key)
        if pr and pr.get("final") is not None and float(pr["final"]) > 0:
            parts.append(f'{emo(slot)} {_num(pr["final"])} {sym}')
    us = d.get("price_us")
    if us and us.get("final") is not None:
        if float(us["final"]) <= 0:
            parts.append(f'{emo("usd")} <b>FREE</b>')
        else:
            parts.append(f'{emo("usd")} ${_usd(us["final"])}')
    return " · ".join(parts)


# ================================================================ МЕТАДАННЫЕ

def _meta_line(details: dict) -> str:
    """Жанры + локализация одной курсивной строкой."""
    genres = [_e(g) for g in (details.get("genres") or [])[:2] if g]
    parts = []
    if genres:
        parts.append(" · ".join(genres))
    lang = (details.get("ru_lang") or "").strip()
    if lang:
        parts.append(f"<code>{_e(lang)}</code>")
    return " · ".join(parts)


def _fmt_exp(exp) -> str:
    if not exp:
        return ""
    try:
        return datetime.fromtimestamp(exp, cfg.tz).strftime("%d.%m в %H:%M")
    except (OSError, ValueError):
        return ""


# ================================================================ ФУТЕР

CTA_DEFAULT = "Понравилось? Поставьте реакцию 👇"


def _cta(text: str = "") -> str:
    return f"<i>👍 {_e(text or CTA_DEFAULT)}</i>"


def _footer() -> str:
    return (f'{emo("footer")} <b><a href="https://t.me/{cfg.channel_handle}">'
            f'{_e(cfg.channel_title)}</a></b>')


def _tail(extra: str = "", cta: str = "") -> str:
    """Хвост поста: необязательная строка + призыв к реакции + подпись канала."""
    out = "\n"
    if extra:
        out += extra + "\n"
    return out + _cta(cta) + "\n" + _footer()


# ================================================================ СБОРКА

def _join_limited(head: str, blocks: list[str], tail: str, limit: int) -> tuple[str, int]:
    """Собирает пост, укладываясь в лимит подписи. Возвращает (текст, сколько блоков влезло)."""
    out = head
    used = 0
    for b in blocks:
        if vlen(out) + vlen(b) + vlen(tail) > limit:
            break
        out += b
        used += 1
    if used == 0 and blocks:
        room = max(0, limit - vlen(out) - vlen(tail))
        out += blocks[0][:room]
        used = 1
    return out + tail, used


def _game_block(g, d: dict, note: str = "") -> str:
    """Один блок подборки: название + скидка, ниже все валюты."""
    name = _e(d.get("name") or getattr(g, "title", ""))
    url = d.get("store_url") or f"https://store.steampowered.com/app/{g.appid}/"
    disc = int(getattr(g, "discount", 0) or 0)
    head = f'{emo("tag")} <b><a href="{url}">{name}</a></b>'
    if disc > 0:
        head += f'  <code>−{disc}%</code>'
    line2 = _price_inline(d)
    if note:
        line2 = (line2 + " · " if line2 else "") + f"<i>{_e(note)}</i>"
    body = head if not line2 else f"{head}\n{line2}"
    return f"<blockquote>{body}</blockquote>\n"


# ================================================================ ПОСТЫ

def single_post(game, details: dict, kind: str = "single") -> str:
    """Строгий премиальный одиночный пост.

    {fire} Название  −50%
    жанры · локализация
    <blockquote>одно чистое предложение</blockquote>
    🇷🇺 849 ₽  1 699 ₽
    🇰🇿 … ₸ / 🇺🇦 … ₴ / 🇺🇸 $…
    ⏳ До 31.10 в 22:00
    """
    name = _e(details.get("name") or getattr(game, "title", ""))
    url = details.get("store_url") or f"https://store.steampowered.com/app/{game.appid}/"
    disc = int(getattr(game, "discount", 0) or 0)

    L = [f'{emo("fire")} <b><a href="{url}">{name}</a></b>  <code>−{disc}%</code>']
    meta = _meta_line(details)
    if meta:
        L.append(f"<i>{meta}</i>")

    sent = one_sentence(details)
    if sent:
        L += ["", f"<blockquote>{sent}</blockquote>"]

    prices = price_lines(details)
    if prices:
        L += ["", *prices]

    exp = _fmt_exp(details.get("exp_date"))
    if exp:
        L += ["", f'{emo("clock")} <i>До {exp}</i>']

    L += ["", _cta(), _footer()]
    return "\n".join(L)


def multi_post(games: list, details_map: dict, title: str = "ВЫГОДНАЯ ПОДБОРКА",
               kind: str = "multi") -> tuple[str, int]:
    games = sorted(games, key=lambda x: -x.discount)
    head = f'{emo("fire")} <b>{_e(title)}</b>\n\n'
    blocks = [_game_block(g, details_map.get(g.appid, {})) for g in games]
    return _join_limited(head, blocks, _tail(cta="Зашло? Поставьте реакцию 👇"),
                         cfg.caption_limit)


def series_post(franchise: str, games: list, details_map: dict) -> tuple[str, int]:
    games = sorted(games, key=lambda x: -x.discount)
    head = f'{emo("fire")} <b>СКИДКИ НА СЕРИЮ: {_e(franchise.upper())}</b>\n\n'
    blocks = [_game_block(g, details_map.get(g.appid, {})) for g in games]
    return _join_limited(head, blocks, _tail(cta="Нужны ещё серии? Поставьте реакцию 👇"),
                         cfg.caption_limit)


def roundup_post(games: list, details_map: dict) -> tuple[str, int]:
    games = sorted(games, key=lambda x: (-x.discount, -getattr(x, "score", 0)))
    head = f'{emo("fire")} <b>НОВЫЕ СКИДКИ НА СЕГОДНЯ</b>\n\n'
    blocks = [_game_block(g, details_map.get(g.appid, {})) for g in games]
    return _join_limited(head, blocks, _tail(cta="Полезно? Поставьте реакцию 👇"),
                         cfg.caption_limit)


def ending_post(games: list, details_map: dict, deadline: str) -> tuple[str, int]:
    games = sorted(games, key=lambda x: -x.discount)
    head = f'{emo("clock")} <b>СКИДКИ, КОТОРЫЕ СКОРО ЗАКОНЧАТСЯ</b>\n\n'
    blocks = [_game_block(g, details_map.get(g.appid, {})) for g in games]
    tail = _tail(f'{emo("clock")} <i>Финиш: {_e(deadline)} МСК</i>',
                 cta="Успеваете? Поставьте реакцию 👇")
    return _join_limited(head, blocks, tail, cfg.caption_limit)


def under300_post(games: list, details_map: dict) -> tuple[str, int]:
    games = sorted(games, key=lambda x: -x.discount)
    head = f'{emo("gem")} <b>ХИТЫ ДЕШЕВЛЕ 300 ₽</b>\n\n'
    blocks = [_game_block(g, details_map.get(g.appid, {})) for g in games]
    return _join_limited(head, blocks, _tail(cta="Берёте? Поставьте реакцию 👇"),
                         cfg.caption_limit)


def freebie_post(games: list, details_map: dict) -> tuple[str, int]:
    games = sorted(games, key=lambda x: -x.discount)
    head = f'{emo("gift")} <b>ХАЛЯВА В STEAM</b>\n\n'
    blocks = [_game_block(g, details_map.get(g.appid, {}), note="забирай бесплатно")
              for g in games]
    return _join_limited(head, blocks, _tail(cta="Забрали? Поставьте реакцию 👇"),
                         cfg.caption_limit)


# ---------------------------------------------------------------- новинки/релизы

def _release_block(item: dict, d: dict) -> str:
    """Один релиз: название, дата/статус, жанр, цены и одно предложение сути."""
    name = _e(d.get("name") or item.get("title") or "")
    url = d.get("store_url") or f"https://store.steampowered.com/app/{item.get('appid')}/"

    from steam import format_release  # локальный импорт: избегаем цикла модулей

    # дату показываем, только если Steam отдал её в разбираемом виде
    # («Скоро выйдет» без числа — не дата, выводим просто статус)
    ts = d.get("release_ts")
    when = format_release(ts, "") if ts else ""
    if d.get("coming_soon"):
        status = f"🗓 <i>скоро · {_e(when)}</i>" if when else "🗓 <i>скоро</i>"
    else:
        status = f"🗓 <i>{_e(when)}</i>" if when else "✅ <i>уже в продаже</i>"

    genres = " · ".join(_e(x) for x in (d.get("genres") or [])[:2] if x)
    line2 = " · ".join(p for p in (status, genres) if p)

    # эмодзи рубрики уже стоит в заголовке поста — в блоке он лишний
    body = [f'<b><a href="{url}">{name}</a></b>', line2]
    prices = _price_inline(d)
    if prices:
        body.append(prices)
    sent = one_sentence(d)
    if sent:
        body.append(f"<i>{sent}</i>")
    return "<blockquote>" + "\n".join(body) + "</blockquote>\n"


def releases_post(items: list, details_map: dict, kind: str = "releases") -> tuple[str, int]:
    """Новинки и скорые релизы: сухо, по делу, без скидочной бухгалтерии."""
    head = f'{emo("news")} <b>НОВИНКИ И РЕЛИЗЫ</b>\n\n'
    blocks = [_release_block(it, details_map.get(it["appid"], {})) for it in items]
    return _join_limited(head, blocks, _tail(cta="Что ждёте? Поставьте реакцию 👇"),
                         cfg.caption_limit)


# ---------------------------------------------------------------- сезонные события

def event_post(event, games: list, details_map: dict) -> tuple[str, int]:
    """Тематическая подборка активного события (Хэллоуин, распродажа и т.д.)."""
    games = sorted(games, key=lambda x: -x.discount)
    head = (f'{emo(event.emoji)} <b>{_e(event.title)}</b>\n'
            f'<i>{_e(event.tagline)}</i>\n\n')
    blocks = [_game_block(g, details_map.get(g.appid, {})) for g in games]
    return _join_limited(head, blocks, _tail(cta="Зашло? Поставьте реакцию 👇"),
                         cfg.caption_limit)


# ================================================================ ФРАНШИЗЫ

_EDITION_RE = re.compile(
    r"(\s+(deluxe|definitive|game of the year|gold|collector'?s?|ultimate|complete|standard|"
    r"remastered?|remake|enhanced|anniversary|limited|digital|special|premium|edition|dlc|season pass)"
    r"\s*(\d*))$",
    re.I,
)
_NUM_SUFFIX_RE = re.compile(r"\s+(i{1,3}|iv|v{1,3}|ix|x{1,3}|xl|l|2|3|4|5|6|7|8|9|10|11|12)\s*$", re.I)


def franchise_of(title: str) -> str:
    t = re.sub(r"[®™©]", "", (title or "").strip())
    t = re.split(r"\s*[:—–-]\s*", t)[0].strip()
    t = _EDITION_RE.sub("", t).strip()
    t = _NUM_SUFFIX_RE.sub("", t).strip()
    return re.sub(r"\s+", " ", t).lower()


def group_franchises(games: list) -> dict:
    out: dict = {}
    for g in games:
        key = franchise_of(g.title)
        if len(key) < 4:
            key = (g.title or "").lower()
        out.setdefault(key, []).append(g)
    return {k: v for k, v in out.items() if len(v) >= 2}
