"""Оркестратор: sync скидок -> выбор формата -> карточка -> отправка в ТГ.

ВАЖНО: в пуле остаются ВСЕ игры со скидками (без отсева «только популярные»).
Вес score используется ТОЛЬКО для порядка очереди, а не для исключения игр.
Сезонные события (events.py) поднимают тематические игры в начало очереди
и ставят праздничную плашку на карточку.
"""
import json
import logging
import os
import random
import re
import shutil
import threading
import time
from datetime import datetime

import requests

import events
import news as news_mod
import security
import templates
from config import cfg
from images import make_card, make_multi_card, make_news_card
from steam import (compute_score, download_image, fetch_game_details,
                   search_specials, search_upcoming_and_new)
from storage import Game, Storage

security.install()
log = logging.getLogger("poster")

API = "https://api.telegram.org/bot{token}/{method}"
TG_EMOJI_RE = re.compile(r"</?tg-emoji[^>]*>", re.I)
TG_EMOJI_FULL_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.I | re.S)
BLOCKQUOTE_RE = re.compile(r"</?blockquote[^>]*>", re.I)
CODE_RE = re.compile(r"</?code>", re.I)
ALL_TAGS_RE = re.compile(r"</?(?:b|strong|i|em|u|ins|s|strike|del|a|span|pre)[^>]*>", re.I)

SKIP_TYPES = {"demo", "music", "video", "series", "mod", "advertising", "hardware", "config"}

_syncing = False
_freebie_lock = False

def _mask(text) -> str:
    return cfg.mask(text)


def _storage() -> Storage:
    return Storage(cfg.db_path)


# ---------------- сезонные события ----------------

def _event():
    """Активное сезонное событие (или None)."""
    try:
        return events.active_event()
    except Exception:  # noqa
        return None


# рубрики, на карточках которых ставим праздничную плашку
_DISCOUNT_KINDS = {"single", "multi", "series", "roundup", "ending", "under300", "freebie"}


def _card_kind(kind: str) -> str:
    """Плашка активного события вместо обычной плашки рубрики."""
    ev = _event()
    if ev and kind in _DISCOUNT_KINDS:
        return ev.key
    return kind


def _event_priority(games: list[Game]) -> list[Game]:
    """Тематические игры — в начало очереди. Никакого отсева, только порядок."""
    ev = _event()
    if not ev or not ev.keywords:
        return games
    hot = [g for g in games if events.is_thematic(getattr(g, "title", ""), ev)]
    hot_ids = {g.appid for g in hot}
    return hot + [g for g in games if g.appid not in hot_ids]


def _tg(method: str, **kwargs):
    try:
        r = requests.post(API.format(token=cfg.bot_token, method=method), timeout=40, **kwargs)
        data = r.json()
        if not data.get("ok"):
            log.error("Telegram %s failed: %s", method, _mask(data))
            return None
        return data.get("result")
    except requests.RequestException as e:
        log.error("Telegram %s error: %s", method, _mask(e))
        return None


# ---------------- отправка фото с каскадным фолбэком ----------------

def _send_photo_raw(caption: str, photo_path: str, reply_markup: dict | None):
    data = {"chat_id": cfg.channel, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    with open(photo_path, "rb") as f:
        r = requests.post(API.format(token=cfg.bot_token, method="sendPhoto"),
                          data=data, files={"photo": f}, timeout=90)
    return r


def _trim_caption(caption: str) -> str:
    """Подпись к фото в Telegram — максимум 1024 символа (в единицах UTF-16).

    Режем по строкам, чтобы не порвать HTML-тег, и проверяем, что все
    открытые теги закрыты — иначе Telegram вернёт ошибку разметки.
    """
    limit = min(int(cfg.caption_limit), 1024)
    if templates.vlen(caption) <= limit:
        return caption
    lines = caption.split("\n")
    kept: list[str] = []
    for ln in lines:
        candidate = "\n".join(kept + [ln])
        if templates.vlen(candidate) > limit:
            break
        kept.append(ln)
    while kept and not templates.html_balanced("\n".join(kept)):
        kept.pop()
    return "\n".join(kept).rstrip()


# Сериализация публикаций: планировщик и ручная команда никогда не возьмут
# одну и ту же игру параллельно.
POST_LOCK = threading.Lock()


def _replace_tg_emoji(caption: str) -> str:
    """Заменяет <tg-emoji ...>🔥</tg-emoji> на обычный 🔥 внутри тега."""
    return TG_EMOJI_RE.sub("", TG_EMOJI_FULL_RE.sub(lambda m: m.group(1) or "", caption))


def send_photo(caption: str, photo_path: str, reply_markup: dict | None = None) -> bool:
    if not cfg.bot_token:
        log.warning("Нет BOT_TOKEN, пост не отправлен")
        return False
    caption = _trim_caption(caption)
    # каскад: полный HTML -> обычные эмодзи вместо tg-emoji -> без blockquote -> текст
    no_tg = _replace_tg_emoji(caption)
    variants = [
        ("полный", caption),
        ("эмодзи вместо tg-emoji", no_tg),
        ("без blockquote/code", ALL_TAGS_RE.sub("", BLOCKQUOTE_RE.sub("", CODE_RE.sub("", no_tg)))),
        ("plain text", ALL_TAGS_RE.sub("", BLOCKQUOTE_RE.sub("", CODE_RE.sub("", no_tg)))),
    ]
    last_err = ""
    for i, (name, text) in enumerate(variants):
        try:
            r = _send_photo_raw(text, photo_path, reply_markup)
            resp = r.json()
        except requests.RequestException as e:
            log.error("sendPhoto request error: %s", _mask(e))
            return False
        if resp.get("ok"):
            msg_id = (resp.get("result") or {}).get("message_id")
            log.info("Пост в канале: message_id=%s, вариант «%s»", msg_id, name)
            if i:
                log.warning("sendPhoto отправлен вариантом «%s» (причина: %s)", name, _mask(last_err))
            return True
        last_err = resp.get("description") or str(resp)
        low = last_err.lower()
        if "document_invalid" in low or "document invalid" in low:
            log.warning("sendPhoto: DOCUMENT_INVALID — Telegram не принимает emoji-id "
                        "(боту нужна подписка Premium/Fragment или неверный CUSTOM_EMOJI_MAP)")
        elif "entit" in low or "parse" in low or "tag" in low:
            log.warning("sendPhoto: ошибка разметки HTML — %s", _mask(last_err))
        else:
            log.error("sendPhoto failed: %s", _mask(last_err))
            if "chat not found" in low or "not enough rights" in low:
                return False
    return False


# ---------------- sync ----------------

def sync() -> int:
    global _syncing
    if _syncing:
        return 0
    _syncing = True
    try:
        log.info("Синхронизация скидок Steam...")
        games = search_specials()
        st = _storage()
        for g in games:
            st.upsert_seen(g)
        log.info("Синхронизировано %d игр (всего в базе %d)", len(games), st.seen_count())
        st.kv_set_json("last_sync", {"last_sync": int(time.time()), "games": len(games), "total": st.seen_count()})

        # раз в сутки подчищаем рабочие картинки, чтобы не забить диск сервера
        maybe_cleanup()

        # ХАЛЯВА (100%) — высший приоритет: публикуем сразу после синхронизации
        free = [g for g in games if g.discount >= 100]
        if free and not _freebie_lock:
            log.info("Найдена халява (100%%): %d игр — публикую с приоритетом", len(free))
            try:
                post_freebie()
            except Exception as e:  # noqa
                log.warning("post_freebie failed: %s", _mask(e))
        return len(games)
    finally:
        _syncing = False


def _ensure_candidates(n: int = 3):
    """Быстрая подкачка свежих скидок, если очередь кандидатов пустеет."""
    st = _storage()
    if len(st.candidates(cfg.min_discount, cfg.min_score)) < n:
        log.info("Мало кандидатов — быстрая подкачка скидок")
        sync()


def _pick(games: list[Game], n: int, exclude: set[int] | None = None,
          mark: bool = True, shuffle: bool = True) -> list[Game]:
    """Выбирает n игр и СРАЗУ фиксирует их в posted (стопроцентная защита от повторов).

    Топ-40 неопубликованных кандидатов перемешиваются, чтобы бот не брал одну и
    ту же верхнюю строку при каждом запуске. Как только игра выбрана — она
    попадает в таблицу posted и больше никогда не будет взята снова.
    """
    exclude = exclude or set()
    st = _storage()
    pool = [g for g in games if g.appid not in exclude and not st.is_posted(g.appid)]
    if shuffle and len(pool) > 1:
        head = pool[:40]
        random.shuffle(head)
        pool = head + pool[40:]
    out: list[Game] = []
    for g in pool:
        if len(out) >= n:
            break
        out.append(g)
        if mark:
            st.mark_skipped(g.appid, "picked")
    return out


def _enrich(games: list[Game]) -> dict[int, dict]:
    details = {}
    st = _storage()
    for g in games:
        try:
            d = fetch_game_details(g.appid)
        except Exception as e:  # noqa
            log.warning("details %s: %s", g.appid, _mask(e))
            st.mark_skipped(g.appid, "details-error")
            continue
        if not d or d.get("type") in SKIP_TYPES:
            # пустой ответ Steam — игру больше не трогаем, иначе она зациклит выбор
            st.mark_skipped(g.appid, "details-empty")
            continue
        # обновляем вес игры по свежим данным (только для порядка очереди)
        sc = compute_score(
            getattr(g, "review_pct", 0),
            d.get("reviews_total") or getattr(g, "review_count", 0),
            g.discount,
            d.get("metacritic"),
        )
        d["score"] = sc
        try:
            st.set_score(g.appid, sc)
        except Exception:  # noqa
            pass
        details[g.appid] = d
        time.sleep(cfg.steam_delay)
    return details


def _prices_for_card(details: dict) -> dict:
    return {
        "RUB": details.get("price_ru") or {},
        "KZT": details.get("price_kz") or {},
        "UAH": details.get("price_ua") or {},
        "USD": details.get("price_us") or {},
    }


def _raw_art(g: Game, details: dict) -> str:
    raw = os.path.join(cfg.img_dir, f"raw_{g.appid}.jpg")
    if not (os.path.exists(raw) and os.path.getsize(raw) > 5000):
        if not download_image(details.get("header_image", ""), raw):
            return ""
    return raw


def _card(g: Game, details: dict, kind: str, title: str = "") -> str:
    os.makedirs(cfg.img_dir, exist_ok=True)
    raw = _raw_art(g, details)
    if not raw:
        return ""
    kind = _card_kind(kind)
    out = os.path.join(cfg.img_dir, f"card_{g.appid}_{kind}.jpg")
    if not (os.path.exists(out) and os.path.getsize(out) > 8000):
        make_card(raw, out, cfg.channel_handle, g.discount,
                  prices=_prices_for_card(details),
                  title=title or details.get("name") or g.title,
                  kind=kind, genres=details.get("genres") or [])
    return out if os.path.exists(out) else ""


def _multi_card(games: list[Game], details: dict, kind: str) -> str:
    """Мульти-карточка (сетка) для ВСЕХ групповых рубрик."""
    kind = _card_kind(kind)
    cards_data = []
    for g in games:
        d = details.get(g.appid, {})
        cards_data.append({
            "header": _raw_art(g, d),
            "title": d.get("name") or g.title,
            "discount": g.discount,
            "price_ru": d.get("price_ru"),
            "price_kz": d.get("price_kz"),
            "price_ua": d.get("price_ua"),
            "price_us": d.get("price_us"),
        })
    out = os.path.join(cfg.img_dir, f"{kind}_{int(time.time())}.jpg")
    return out if make_multi_card(cards_data, out, cfg.channel_handle, kind) else ""


# ---------------- кнопки ----------------

def _keyboard(games: list[Game], details: dict, kind: str = "single") -> dict | None:
    if not games:
        return None
    if kind == "single":
        g = games[0]
        d = details.get(g.appid, {})
        url = d.get("store_url") or f"https://store.steampowered.com/app/{g.appid}/"
        return {"inline_keyboard": [[
            {"text": "🛒 Открыть в Steam", "url": url},
            {"text": "📊 История цен (SteamDB)", "url": f"https://steamdb.info/app/{g.appid}/"},
        ]]}
    btns = []
    for g in games:
        d = details.get(g.appid, {})
        url = d.get("store_url") or f"https://store.steampowered.com/app/{g.appid}/"
        name = (d.get("name") or g.title).strip()
        if len(name) > 34:
            name = name[:33].rstrip() + "…"
        btns.append({"text": f"🔥 {name} (−{g.discount}%)", "url": url})
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    return {"inline_keyboard": rows} if rows else None


# ---------------- автоочистка диска ----------------

_IMG_KEEP_PREFIX = ("avatar_", "test_")
_IMG_WORK_PREFIX = ("raw_", "card_", "multi_")


def cleanup_images() -> int:
    """Удаляет рабочие картинки из images/ (raw_*, card_*, multi_* и т.п.).

    Оставляет только аватарки (avatar_*) и тестовые превью (test_*) — на сервере
    с маленьким диском папка images/ не должна расти бесконечно. Рабочие префиксы
    удаляются при ЛЮБОМ расширении (Steam иногда отдаёт .webp/.png).
    """
    removed = 0
    try:
        names = os.listdir(cfg.img_dir)
    except OSError:
        return 0
    for name in names:
        low = name.lower()
        if low.startswith(_IMG_KEEP_PREFIX):
            continue
        path = os.path.join(cfg.img_dir, name)
        if not os.path.isfile(path):
            continue
        if not (low.startswith(_IMG_WORK_PREFIX)
                or low.endswith((".jpg", ".jpeg", ".png", ".webp"))):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    if removed:
        log.info("Очистка images/: удалено %d файлов", removed)
    return removed


def _cleanup_after_post(games, card_path: str) -> None:
    """Удаляет картинки только что опубликованного поста (они больше не нужны)."""
    paths = {card_path} if card_path else set()
    for g in games or []:
        paths.add(os.path.join(cfg.img_dir, f"raw_{g.appid}.jpg"))
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def maybe_cleanup(hours: float = 24) -> int:
    """Полная уборка images/ не чаще раза в сутки (для cron-режима)."""
    try:
        st = _storage()
        last = float(st.kv_get("last_cleanup") or 0)
        if time.time() - last < hours * 3600:
            return 0
        n = cleanup_images()
        st.kv_set("last_cleanup", str(int(time.time())))
        return n
    except Exception as e:  # noqa
        log.warning("cleanup failed: %s", _mask(e))
        return 0


def _post_games(games, ptype, caption, card_path, markup=None, mark=True) -> bool:
    if not games:
        log.info("Нет игр для %s — пропускаю", ptype)
        return False
    if card_path and send_photo(caption, card_path, reply_markup=markup):
        if mark:
            _storage().mark_posted([g.appid for g in games], ptype)
        log.info("Опубликован %s: %s", ptype, ", ".join(g.title for g in games))
        _cleanup_after_post(games, card_path)
        return True
    if not card_path:
        log.warning("Картинка не собралась для %s", ptype)
    return False


# ---------------- форматы ----------------

def _post_single_impl() -> bool:
    """Одиночный пост: игра ОБЯЗАТЕЛЬНО с русским описанием в 1–2 предложения."""
    _ensure_candidates(6)
    st = _storage()
    for _ in range(10):
        cand = _pick(_event_priority(st.candidates(cfg.min_discount, cfg.min_score)), 1)
        if not cand:
            log.warning("Кандидаты кончились — single опубликовать нечем")
            return False
        game = cand[0]
        details = _enrich([game]).get(game.appid)
        if not details:
            continue  # _enrich уже пометил игру как skipped
        sent = templates.description(details)
        if not sent:
            log.info("У %s нет русского описания — берём следующую", game.title)
            st.mark_skipped(game.appid, "no-desc")
            continue
        caption = templates.single_post(game, details)
        card = _card(game, details, "single", details.get("name") or game.title)
        if not card:
            log.warning("Карточка для %s не собралась", game.title)
            return False
        return _post_games([game], "single", caption, card,
                           markup=_keyboard([game], details, "single"))
    log.warning("Не нашли игру с описанием для single")
    return False


def _post_multi_impl() -> bool:
    """Подборка РОВНО из 4 игр: мульти-карточка + список с ценами в 4 валютах."""
    _ensure_candidates(8)
    cands = _pick(_event_priority(_storage().candidates(cfg.min_discount, cfg.min_score)), 4)
    if len(cands) < 4:
        log.info("Меньше 4 кандидатов — публикуем одиночный пост")
        return _post_single_impl()  # уже внутри POST_LOCK
    details = _enrich(cands)
    games = [g for g in cands if g.appid in details]
    if len(games) < 4:
        log.info("Не у всех игр есть детали (%d из 4) — одиночный пост", len(games))
        return _post_single_impl()
    caption, used = templates.multi_post(games, details)
    games = games[:used]
    card = _multi_card(games, details, "multi")
    if not card:
        log.warning("Мульти-карточка не собралась")
        return False
    return _post_games(games, "multi", caption, card,
                       markup=_keyboard(games, details, "group"))


def _post_news_impl() -> bool:
    """Игровая новость дня: свежий RSS-материал + карточка «НОВОСТИ ИГР»."""
    st = _storage()
    article = news_mod.pick_article(st.is_news_posted)
    if not article:
        log.warning("Свежих неопубликованных новостей нет")
        return False

    caption = templates.news_post(article)
    img = news_mod.image_path(article)
    out = os.path.join(cfg.img_dir, f"card_news_{article['key']}.jpg")
    if not make_news_card(img, out, cfg.channel_handle,
                          article.get("title", ""), article.get("source", "")):
        log.warning("Карточка новости не собралась")
        return False

    markup = {"inline_keyboard": [[{"text": "📖 Читать подробнее", "url": article["url"]}]]}
    if not send_photo(caption, out, reply_markup=markup):
        return False

    st.mark_news_posted(article["key"], article["url"], article["title"])
    log.info("Опубликована новость: %s (%s)", article["title"], article.get("source"))
    for p in (out, img):
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return True


def post_single() -> bool:
    with POST_LOCK:
        return _post_single_impl()


def post_multi() -> bool:
    with POST_LOCK:
        return _post_multi_impl()


def post_news() -> bool:
    with POST_LOCK:
        return _post_news_impl()


def post_series() -> bool:
    _ensure_candidates(6)
    cands = _pick(_storage().candidates(cfg.min_discount, cfg.min_score), 30)
    groups = templates.group_franchises(cands) if cands else {}
    if not groups:
        log.info("Нет серий — фолбэк на multi/single")
        return post_multi() or post_single()
    franchise = max(groups, key=lambda k: sum(g.discount for g in groups[k]))
    games = sorted(groups[franchise], key=lambda x: -x.discount)[:8]
    details = _enrich(games)
    games = [g for g in games if g.appid in details]
    if len(games) < 2:
        return post_multi() or post_single()
    caption, used = templates.series_post(franchise, games, details)
    games = games[:used]
    card = _multi_card(games, details, "series")
    if not card:
        return post_multi() or post_single()
    return _post_games(games, "series", caption, card,
                       markup=_keyboard(games, details, "group"))


def post_roundup() -> bool:
    _ensure_candidates(8)
    st = _storage()
    new = st.new_since(24)
    games = _pick(_event_priority(new), 12) if new else []
    if len(games) < 3:
        log.info("Мало новых за 24ч — берём топ кандидатов")
        games = _pick(_event_priority(st.candidates(cfg.min_discount, cfg.min_score)), 10)
    if not games:
        return post_single()
    details = _enrich(games)
    games = [g for g in games if g.appid in details]
    if len(games) < 2:
        return post_single()
    caption, used = templates.roundup_post(games, details)
    games = games[:used]
    card = _multi_card(games, details, "roundup")
    if not card:
        return post_single()
    return _post_games(games, "roundup", caption, card,
                       markup=_keyboard(games, details, "group"))


def post_ending() -> bool:
    _ensure_candidates(4)
    st = _storage()
    cands = _pick(st.candidates(cfg.min_discount, cfg.min_score), 24)
    enders = []
    for g in cands[:14]:
        try:
            d = fetch_game_details(g.appid)
        except Exception:  # noqa
            continue
        time.sleep(cfg.steam_delay)
        if d and d.get("exp_date"):
            exp = d["exp_date"]
            now = int(time.time())
            if 0 < exp - now <= 30 * 3600:
                enders.append((g, exp, d))
        if len(enders) >= 6:
            break
    if len(enders) < 2:
        log.info("Мало заканчивающихся — фолбэк на multi")
        return post_multi() or post_single()
    deadline_ts = min(e for _, e, _ in enders)
    deadline = datetime.fromtimestamp(deadline_ts, cfg.tz).strftime("%d.%m %H:%M")
    games = [g for g, _, _ in sorted(enders, key=lambda x: x[1])]
    details = {g.appid: d for g, _, d in enders}
    caption, used = templates.ending_post(games, details, deadline)
    games = games[:used]
    card = _multi_card(games, details, "ending")
    if not card:
        return post_multi() or post_single()
    return _post_games(games, "ending", caption, card,
                       markup=_keyboard(games, details, "group"))


def post_under300() -> bool:
    """💎 Хиты дешевле 300 ₽ (по цене в рублях / по курсу)."""
    _ensure_candidates(8)
    st = _storage()
    cands = _pick(st.candidates(cfg.min_discount, cfg.min_score), 40)
    picked, details = [], {}
    for g in cands[:12]:
        if len(picked) >= 4:
            break
        d = _enrich([g]).get(g.appid)
        if not d:
            continue
        pr = d.get("price_ru") or {}
        if pr.get("final") is not None and 0 < float(pr["final"]) <= 300:
            picked.append(g)
            details[g.appid] = d
    if len(picked) < 2:
        log.info("Мало игр дешевле 300 ₽ — фолбэк на multi")
        return post_multi() or post_single()
    caption, used = templates.under300_post(picked, details)
    games = picked[:used]
    card = _multi_card(games, details, "under300")
    if not card:
        return post_multi() or post_single()
    return _post_games(games, "under300", caption, card,
                       markup=_keyboard(games, details, "group"))


def post_freebie() -> bool:
    """🎁 Халява в Steam: платная игра со скидкой 100%."""
    global _freebie_lock
    if _freebie_lock:
        return False
    _freebie_lock = True
    try:
        st = _storage()
        cands = [g for g in st.candidates(100, 0) if g.discount >= 100]
        if not cands:
            log.info("Халявы сейчас нет")
            return False
        games = _pick(cands, 4)
        details = _enrich(games)
        games = [g for g in games if g.appid in details]
        if not games:
            return False
        caption, used = templates.freebie_post(games, details)
        games = games[:used]
        card = _multi_card(games, details, "freebie")
        if not card:
            return False
        return _post_games(games, "freebie", caption, card,
                           markup=_keyboard(games, details, "group"))
    finally:
        _freebie_lock = False


def post_event() -> bool:
    """Тематическая подборка активного события (Хэллоуин, распродажа и т.д.)."""
    ev = _event()
    if not ev:
        log.info("Сезонного события нет — фолбэк на multi")
        return post_multi() or post_single()

    st = _storage()
    pool = _pick(st.candidates(cfg.min_discount, cfg.min_score), 60)
    if not pool:
        return post_multi() or post_single()

    # для распродаж — только крупные скидки (если таких достаточно)
    if ev.min_discount:
        strong = [g for g in pool if g.discount >= ev.min_discount]
        pool = strong or pool

    hot = [g for g in pool if events.is_thematic(g.title, ev)]
    hot_ids = {g.appid for g in hot}
    shortlist = (hot + [g for g in pool if g.appid not in hot_ids])[:14]

    picked, details = [], {}
    for g in shortlist:
        if len(picked) >= 4:
            break
        try:
            d = fetch_game_details(g.appid)
        except Exception as e:  # noqa
            log.warning("details %s: %s", g.appid, _mask(e))
            continue
        if not d or d.get("type") in SKIP_TYPES:
            continue
        if events.game_is_thematic(d, ev):
            picked.append(g)
            details[g.appid] = d
        elif len(picked) < 2:
            # добивка, чтобы подборка не осталась из одной игры
            picked.append(g)
            details[g.appid] = d
        time.sleep(cfg.steam_delay)

    if len(picked) < 2:
        log.info("Мало тематических игр — фолбэк на multi")
        return post_multi() or post_single()

    caption, used = templates.event_post(ev, picked, details)
    games = picked[:used]
    card = _multi_card(games, details, ev.key)
    if not card:
        return post_multi() or post_single()
    return _post_games(games, ev.key, caption, card,
                       markup=_keyboard(games, details, "group"))


# ---------------- новинки и релизы ----------------

RELEASES_MEMORY_DAYS = 14


def _releases_seen() -> dict[int, float]:
    raw = _storage().kv_get_json("releases_posted", {}) or {}
    cutoff = time.time() - RELEASES_MEMORY_DAYS * 86400
    out: dict[int, float] = {}
    for k, v in raw.items():
        try:
            ts = float(v)
            if ts >= cutoff:
                out[int(k)] = ts
        except (TypeError, ValueError):
            continue
    return out


def post_releases() -> bool:
    """Новинки и скорые релизы: 1 раз в день, чтобы скидки остались основой канала."""
    try:
        data = search_upcoming_and_new()
    except Exception as e:  # noqa
        log.warning("search_upcoming_and_new failed: %s", _mask(e))
        data = {}
    items = (data.get("coming_soon") or []) + (data.get("new_releases") or [])
    if not items:
        log.info("Steam не отдал новинки — фолбэк на roundup")
        return post_roundup() or post_single()

    seen = _releases_seen()
    fresh = [it for it in items if it["appid"] not in seen]
    if len(fresh) < 3:
        fresh = items  # всё уже показывали — берём заново, но свежий порядок

    # тянем детали, чтобы получить дату релиза, жанры и цены
    collected = []
    for it in fresh[:8]:
        try:
            d = fetch_game_details(it["appid"])
        except Exception as e:  # noqa
            log.warning("details %s: %s", it["appid"], _mask(e))
            continue
        if not d or d.get("type") in SKIP_TYPES:
            continue
        collected.append((it, d))
        time.sleep(cfg.steam_delay)

    if len(collected) < 2:
        log.info("Мало данных по новинкам — фолбэк на roundup")
        return post_roundup() or post_single()

    # скорые релизы (по дате) вперёд, затем уже вышедшие
    collected.sort(key=lambda x: (
        0 if x[1].get("coming_soon") else 1,
        x[1].get("release_ts") or 9_999_999_999,
    ))
    chosen = collected[:4]

    details = {it["appid"]: d for it, d in chosen}
    items4 = [it for it, _ in chosen]
    games = [Game(appid=it["appid"], title=d.get("name") or it.get("title") or "",
                  discount=int((d.get("price_ru") or {}).get("discount") or 0),
                  final=(d.get("price_ru") or {}).get("final"),
                  initial=(d.get("price_ru") or {}).get("initial"),
                  currency="RUB")
             for it, d in chosen]

    caption, used = templates.releases_post(items4, details)
    games = games[:used]
    items4 = items4[:used]
    card = _multi_card(games, details, "releases")
    if not card:
        return post_roundup() or post_single()

    ok = _post_games(games, "releases", caption, card,
                     markup=_keyboard(games, details, "group"), mark=False)
    if ok:
        # не выкидываем новинки из скидочной очереди: отдельная память релизов
        memory = _releases_seen()
        now = time.time()
        for it in items4:
            memory[it["appid"]] = now
        _storage().kv_set_json("releases_posted", memory)
    return ok


# ---------------- самодиагностика ----------------

def selfcheck() -> list[dict]:
    """Проверка всех узлов автономности. [{"name", "ok", "detail"}].

    Чек-лист: Telegram Bot API (имя, вебхук, админ в канале), SQLite WAL
    (seen/posted/дубли), курсы валют (USD/KZT/UAH к RUB), тестовая сборка
    всех 3 типов постов (multi, single с описанием, news с проверкой RSS).
    """
    import io

    import currency

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1) Telegram Bot API
    try:
        me = _tg("getMe") or {}
        wh = _tg("getWebhookInfo") or {}
        member = _tg("getChatMember", json={"chat_id": cfg.channel,
                                            "user_id": me.get("id")}) or {}
        status = member.get("status")
        can_post = member.get("can_post_messages")
        ok = bool(me.get("username")) and status in ("administrator", "creator") \
            and can_post is not False
        add("[OK] Telegram Bot API", ok,
            f"бот @%s · вебхук: %s · в канале %s: %s (can_post=%s)" % (
                me.get("username") or "—",
                wh.get("url") or "не установлен",
                cfg.channel, status or "—", can_post))
    except Exception as e:  # noqa
        add("[OK] Telegram Bot API", False, f"ошибка: {_mask(e)}")

    # 2) SQLite WAL: seen, posted, дубли
    try:
        st = _storage()
        mode = (st.conn.execute("PRAGMA journal_mode").fetchone() or [""])[0]
        total = st.seen_count()
        posted = st.posted_count()
        dupes = st.conn.execute(
            "SELECT COUNT(*) FROM (SELECT appid FROM posted GROUP BY appid HAVING COUNT(*)>1)"
        ).fetchone()[0]
        pool = len(st.candidates(cfg.min_discount, cfg.min_score))
        add("[OK] SQLite WAL", str(mode).lower() == "wal" and dupes == 0,
            f"режим {mode} · seen {total} · posted {posted} · дублей {dupes} · пул {pool}")
    except Exception as e:  # noqa
        add("[OK] SQLite WAL", False, f"ошибка: {_mask(e)}")

    # 3) Курсы валют USD/KZT/UAH к RUB
    try:
        rates = currency.get_rates()
        usd = float(rates.get("USD") or 0)
        kzt = float(rates.get("KZT") or 0)
        uah = float(rates.get("UAH") or 0)
        add("[OK] Курсы валют", usd > 0 and kzt > 0 and uah > 0,
            f"1 USD = {usd:.2f} ₽ · 1 KZT = {kzt:.3f} ₽ · 1 UAH = {uah:.2f} ₽")
    except Exception as e:  # noqa
        add("[OK] Курсы валют", False, f"ошибка: {e}")

    # 4) Тестовая генерация всех 3 типов постов (БЕЗ публикации)
    # 4a. multi: 4 кандидата + текст + мульти-карточка
    try:
        st = _storage()
        cands = _pick(st.candidates(cfg.min_discount, cfg.min_score), 4)
        test_ids = [g.appid for g in cands]
        details = _enrich(cands)
        games = [g for g in cands if g.appid in details]
        cap, used = templates.multi_post(games, details) if len(games) >= 3 else ("", 0)
        games = games[:used] if used else games
        card = _multi_card(games, details, "multi") if len(games) >= 3 else ""
        st.conn.execute(
            "DELETE FROM posted WHERE ptype='picked' AND appid IN (%s)" %
            ",".join("?" * len(test_ids)), test_ids)
        st.conn.commit()
        ok = len(games) >= 3 and bool(cap) and bool(card)
        add("[OK] Тест генерации multi", ok,
            f"{len(games)} игр · текст {templates.vlen(cap)} симв. · "
            f"карточка {'есть' if card else 'НЕТ'}")
    except Exception as e:  # noqa
        add("[OK] Тест генерации multi", False, f"ошибка: {e}")

    # 4b. single: блок описания ОБЯЗАТЕЛЕН
    try:
        st = _storage()
        game = details = None
        test_ids: list[int] = []
        for _ in range(8):
            cand = _pick(st.candidates(cfg.min_discount, cfg.min_score), 1)
            if not cand:
                break
            test_ids.append(cand[0].appid)
            d = _enrich(cand).get(cand[0].appid)
            pr = d.get("price_ru") or {}
            if d and (pr.get("final") or 0) > 0:
                sent = templates.description(d)
                if sent:
                    game, details = cand[0], d
                    break
        if not game:
            raise RuntimeError("нет подходящего кандидата")
        desc = templates.description(details)
        cap = templates.single_post(game, details)
        card = _card(game, details, "single", details.get("name") or game.title)
        st.conn.execute(
            "DELETE FROM posted WHERE ptype='picked' AND appid IN (%s)" %
            ",".join("?" * len(test_ids)), test_ids)
        st.conn.commit()
        ok = bool(desc) and 120 <= templates.vlen(desc) <= 220 \
            and "<blockquote>" in cap and bool(card)
        add("[OK] Тест генерации single", ok,
            f"{game.title[:38]} · описание {templates.vlen(desc)} симв. · "
            f"blockquote {'есть' if '<blockquote>' in cap else 'НЕТ'} · "
            f"карточка {'есть' if card else 'НЕТ'}")
        for p in (card,):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception as e:  # noqa
        add("[OK] Тест генерации single", False, f"ошибка: {e}")

    # 4c. news: RSS → статья → подпись → карточка
    try:
        items = news_mod.fetch_all()
        st = _storage()
        art = news_mod.pick_article(st.is_news_posted) if items else None
        cap = templates.news_post(art) if art else ""
        img = news_mod.image_path(art) if art else None
        ok = len(items) > 0 and bool(art) and bool(cap) and bool(img) \
            and "<blockquote>" in cap
        add("[OK] Тест генерации news", ok,
            f"RSS-статей {len(items)} · «{(art or {}).get('title', '')[:45]}» · "
            f"подпись {templates.vlen(cap)} симв. · картинка {'есть' if img else 'НЕТ'}")
        for p in (img,):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception as e:  # noqa
        add("[OK] Тест генерации news", False, f"ошибка: {e}")

    return checks


POSTERS = {
    "single": post_single,
    "multi": post_multi,
    "news": post_news,
    "series": post_series,
    "roundup": post_roundup,
    "ending": post_ending,
    "under300": post_under300,
    "freebie": post_freebie,
    "releases": post_releases,
    "event": post_event,
}

# /post halloween|autumn|winter|spring|summer — тематическая подборка события
for _ev in events.ALL_EVENTS:
    POSTERS.setdefault(_ev.key, post_event)
