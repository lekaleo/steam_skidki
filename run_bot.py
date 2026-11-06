"""Точка входа.

Режимы:
  python run_bot.py          — 24/7 демон: планировщик + команды владельца (VPS/Docker).
  python run_bot.py --once   — одноразовый запуск для cron (GitHub Actions): sync при
                               необходимости + публикация ОДНОГО поста, который сейчас по
                               расписанию. Завершает процесс.
  python run_bot.py --force single|multi|series|roundup|ending|under300|freebie|
                            releases|event|halloween|autumn|winter|spring|summer
                             — принудительно один пост.
  python run_bot.py --selfcheck — самодиагностика автономности (код возврата 1 при ошибках).
"""
import html
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

import requests

import security
import templates
from config import cfg
from poster import (API, POSTERS, sync, _storage, _tg, cleanup_images,
                    build_custom_post, build_game_post, download_telegram_file,
                    publish_custom_preview, publish_game_preview, send_photo)
import moderator

security.install()

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
security.install()  # повесить фильтр и на хендлеры basicConfig

# Облачный режим (GitHub Actions) публикует посты, но НЕ слушает getUpdates —
# иначе локальный бот и облако будут отбирать друг у друга апдейты (409)
# и владелец перестанет получать мгновенные ответы.
NO_COMMANDS = os.environ.get("NO_COMMANDS", "0").strip() == "1"

STALE_H = 2.0  # слот, наступивший больше 2 часов назад, помечается пропущенным

_LAST_MSG: dict[str, float] = {}  # дедупликация одинаковых строк лога


def now() -> datetime:
    return datetime.now(cfg.tz)


def _slot_key(hh, mm, day) -> str:
    return f"done:{day}:{hh:02d}:{mm:02d}"


def sync_if_needed(force=False) -> int:
    st = _storage()
    last = st.kv_get_json("last_sync") or {}
    last_ts = last.get("last_sync", 0)
    seen_empty = st.seen_count() == 0
    if force or seen_empty or time.time() - last_ts > cfg.sync_hours * 3600:
        return sync()
    log.info("Sync не нужен (последний %s)", datetime.fromtimestamp(last_ts, cfg.tz).strftime("%d.%m %H:%M") if last_ts else "никогда")
    return 0


def run_once(force_kind: str | None = None) -> int:
    """Одноразовый режим для serverless-cron: делает ВСЁ за один запуск."""
    cfg.ensure_dirs()
    # 1) накопившиеся сообщения: команды владельцу + модерация комментариев
    try:
        n = process_pending_updates_once()
        if n:
            log.info("Обработано апдейтов Telegram: %d", n)
    except Exception:  # noqa
        log.exception("process_pending_updates_once failed")
    # 2) чистим временные картинки (raw_*.jpg / card_*.jpg / multi_*.jpg),
    #    чтобы диск никогда не забивался
    try:
        n = cleanup_images()
        if n:
            log.info("Убрано временных файлов images/: %d", n)
    except Exception:  # noqa
        log.exception("cleanup_images failed")
    sync_if_needed()
    st = _storage()

    if force_kind:
        ok = POSTERS[force_kind]()
        log.info("Принудительный пост %s: %s", force_kind, ok)
        return 0 if ok else 1

    # 3) слот по расписанию?
    due = _schedule_due()
    if not due:
        log.info("Сейчас нет слота для поста — выходим")
        return 0

    hh, mm, kind = due[0]
    log.info("Слот %02d:%02d тянет пост: %s", hh, mm, kind)
    day = now().strftime("%Y-%m-%d")
    key = _slot_key(hh, mm, day)
    st.kv_set(key, "1")  # флаг ДО поста: при сбое процесса повтор не случится
    try:
        ok = POSTERS[kind]()
    except Exception:  # noqa
        log.exception("Ошибка в посте %s", kind)
        st.kv_del(key)
        return 1
    if ok:
        log.info("✅ Опубликовано")
        return 0
    log.warning("❌ Не удалось опубликовать %s", kind)
    return 1


def _schedule_due() -> list[tuple[int, int, str]]:
    """Слоты, которые уже наступили и ещё НЕ отработали сегодня.

    Возвращает (hh, mm, kind) — каждый слот считается отдельно по своему
    времени, поэтому 15:00 single и 21:00 single никогда не путаются.
    Факт публикации хранится ТОЛЬКО в SQLite (done:YYYY-MM-DD:HH:MM), поэтому
    перезапуск процесса не приводит к повторному посту. Слот, наступивший
    больше STALE_H часов назад, помечается пропущенным — чтобы после простоя
    бот не вываливал все накопившиеся посты разом.
    """
    cur = now()
    day = cur.strftime("%Y-%m-%d")
    st = _storage()
    due: list[tuple[int, int, str]] = []
    for slot in cfg.schedule:
        hh, mm = slot["time"]
        kind = slot["kind"]
        slot_dt = cur.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot_dt > cur:
            continue
        key = _slot_key(hh, mm, day)
        if st.kv_get(key):
            continue
        age_h = (cur - slot_dt).total_seconds() / 3600
        if age_h > STALE_H:
            st.kv_set(key, "missed")
            log.info("Слот %02d:%02d (%s) пропущен: прошло %.1f ч", hh, mm, kind, age_h)
            continue
        due.append((hh, mm, kind))
    return due


def scheduler_loop():
    while True:
        try:
            # первичный и периодический sync — в фоне, без блокировки старта
            sync_if_needed()

            for hh, mm, kind in _schedule_due():
                day = now().strftime("%Y-%m-%d")
                key = _slot_key(hh, mm, day)
                log.info("Сработал слот %02d:%02d (%s)", hh, mm, kind)
                st = _storage()
                st.kv_set(key, "1")  # флаг ДО поста: защита от зацикливания
                try:
                    ok = POSTERS[kind]()
                except Exception:  # noqa
                    log.exception("Ошибка в посте %s", kind)
                    st.kv_del(key)   # сбой — слот можно повторить в следующем цикле
                    ok = False
                if ok:
                    time.sleep(cfg.post_delay)

            time.sleep(20)
        except Exception:  # noqa
            log.exception("Ошибка в цикле планировщика")
            time.sleep(30)


def _status() -> str:
    st = _storage()
    last = st.kv_get_json("last_sync") or {}
    ls = datetime.fromtimestamp(last["last_sync"], cfg.tz).strftime("%d.%m %H:%M") if last.get("last_sync") else "—"
    sched = ", ".join(f"{s['time'][0]:02d}:{s['time'][1]:02d} {s['kind']}" for s in cfg.schedule)
    import events
    return (
        f"📊 Статус {cfg.channel}\n"
        f"Скидок в базе: {st.seen_count()}\n"
        f"Постов опубликовано: {st.posted_count()}\n"
        f"Последний sync: {ls} ({last.get('games', 0)} игр)\n"
        f"Сезон: {_esc(events.describe(events.active_event()))}\n"
        f"Расписание: {sched}"
    )


def run_selfcheck() -> int:
    """python run_bot.py --selfcheck — проверка всех узлов автономности."""
    from poster import selfcheck

    cfg.ensure_dirs()
    checks = selfcheck()
    print("=== САМОДИАГНОСТИКА АВТОНОМНОСТИ ===")
    for c in checks:
        print(("✅" if c["ok"] else "❌"), c["name"], "—", c["detail"])
    bad = [c for c in checks if not c["ok"]]
    print()
    print("ИТОГ:", "всё в порядке ✅" if not bad else f"проблем: {len(bad)} ❌")
    return 0 if not bad else 1


def _drop_webhook() -> None:
    """Сброс вебхука и расширение подписки на ВСЕ типы апдейтов.

    Важно: deleteWebhook сам по себе не сбрасывает allowed_updates, а
    getUpdates с чужим allowed_updates сужает поток — например, до одних
    channel_post, и тогда личка владельца молчит. Поэтому явно снимаем
    ограничение пустым списком (пустой список = все обновления).
    """
    try:
        _tg("deleteWebhook", json={"drop_pending_updates": False})
        # снимаем возможную залипшую подписку (allowed_updates = [] => все типы)
        try:
            requests.post(API.format(token=cfg.bot_token, method="getUpdates"),
                          json={"timeout": 0, "offset": -1, "allowed_updates": []},
                          timeout=30)
        except requests.RequestException:
            pass  # 409 здесь ожидаем, если бот уже держит long-poll
        info = _tg("getWebhookInfo") or {}
        if info.get("url"):
            log.warning("Вебхук всё ещё установлен: %s", info.get("url"))
        else:
            log.info("Вебхук сброшен, подписка на все типы апдейтов — getUpdates")
    except Exception as e:  # noqa
        log.warning("deleteWebhook: %s", e)


def _owner_id() -> int:
    """Владелец: OWNER_ID из .env либо первый, кто написал боту (kv owner_id)."""
    if cfg.owner_id:
        return int(cfg.owner_id)
    try:
        return int(_storage().kv_get("owner_id") or 0)
    except (TypeError, ValueError):
        return 0


def _claim_owner(uid: int, chat: int) -> bool:
    """Автозахват владельца, если OWNER_ID не задан (первый написавший)."""
    if cfg.owner_id or _owner_id():
        return False
    _storage().kv_set("owner_id", str(int(uid)))
    log.info("Владелец бота назначен автоматически: %s", uid)
    _tg("sendMessage", json={
        "chat_id": chat,
        "text": (f"👑 Вы назначены владельцем бота (ID {uid}).\n"
                 "Теперь доступны /status, /check, /post, /sync, /emojis."),
    })
    return True


# ---------------- меню с кнопками (пункт 2) ----------------
BTN_SINGLE = "🎯 Пост: 1 игра"
BTN_MULTI = "📦 Пост: 4 игры"
BTN_NEWS = "📰 Пост: Новость"
BTN_LINK = "🎮 Пост по ссылке/ID"
BTN_CUSTOM = "✍️ Свой пост в канал"
BTN_STATUS = "📊 Статус и проверка"
BTN_EMOJI = "🎨 Настроить эмодзи"
BTN_SYNC = "🔄 Обновить скидки"

MENU_ROWS = [
    [BTN_SINGLE, BTN_MULTI],
    [BTN_NEWS, BTN_LINK],
    [BTN_CUSTOM, BTN_STATUS],
    [BTN_EMOJI, BTN_SYNC],
]
MENU_TEXTS = {b for row in MENU_ROWS for b in row}

_ACK = {
    "single": "⏳ Принял, собираю пост об одной игре...",
    "multi": "⏳ Принял, собираю подборку из 4 игр...",
    "news": "⏳ Принял, беру свежую игровую новость...",
}

_STORE_RE = re.compile(r"store\.steampowered\.com/(?:app|sub)/(\d+)", re.I)
_ID_RE = re.compile(r"^\s*(\d{3,9})\s*$")

_AWAIT_LOCK = threading.Lock()
_AWAIT: dict[int, str] = {}       # chat_id -> "link" | "custom"
_PENDING: dict[str, dict] = {}    # token -> {"type","data","ts"}

# ---- антифлуд: защита канала от спама командами ----
# Даже владелец (или тот, кто получил доступ к его телефону) не должен
# суметь залить канал постами: на тяжёлые действия — пауза.
HEAVY_COOLDOWN = float(os.environ.get("POST_COOLDOWN", "20") or 20)
_LAST_HEAVY = 0.0
_HEAVY_LOCK = threading.Lock()
_MENU_ONLY_TEXTS = {BTN_STATUS, BTN_EMOJI}


def _heavy_allowed() -> float:
    """0 — можно; иначе сколько секунд осталось ждать (антифлуд)."""
    global _LAST_HEAVY
    now = time.time()
    with _HEAVY_LOCK:
        left = HEAVY_COOLDOWN - (now - _LAST_HEAVY)
        if left > 0:
            return left
        _LAST_HEAVY = now
        return 0.0


def _menu() -> dict:
    return {"keyboard": MENU_ROWS, "resize_keyboard": True,
            "is_persistent": True, "one_time_keyboard": False}


def _send(chat: int, text: str, markup: dict | None = None, menu: bool = True) -> None:
    """Ответ владельцу. Клавиатура-меню всегда на месте (кроме inline-кнопок)."""
    data = {"chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if markup is not None:
        data["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    elif menu:
        data["reply_markup"] = json.dumps(_menu(), ensure_ascii=False)
    _tg("sendMessage", json=data)


def _send_preview(chat: int, payload: dict, kind: str) -> None:
    """Превью поста в личку с кнопками «Опубликовать / Отмена»."""
    token = os.urandom(6).hex()
    _PENDING[token] = {"type": kind, "data": payload, "ts": time.time()}
    # чистим протухшие превью (сутки)
    for t, p in list(_PENDING.items()):
        if time.time() - p["ts"] > 86400:
            _PENDING.pop(t, None)
    markup = {"inline_keyboard": [[
        {"text": "🚀 Опубликовать в канал", "callback_data": f"pub:{token}"},
        {"text": "❌ Отмена", "callback_data": f"can:{token}"},
    ]]}
    if not send_photo(payload["caption"], payload["card"],
                      reply_markup=markup, chat_id=chat):
        _send(chat, "❌ Не удалось отправить превью — смотрите logs/bot.log")


def _thread(fn, *args) -> None:
    def run():
        try:
            fn(*args)
        except Exception:  # noqa
            log.exception("Фоновая задача упала: %s", getattr(fn, "__name__", fn))
    threading.Thread(target=run, daemon=True).start()


# ---------------- действия кнопок ----------------

def _act_kind(chat: int, kind: str) -> None:
    """Пост готовой рубрики: ack сразу, сборка и публикация в фоне."""
    left = _heavy_allowed()
    if left:
        _send(chat, f"🛑 Слишком часто. Подождите {int(left) + 1} с — "
                    "это защита канала от случайного спама.")
        return
    _send(chat, _ACK.get(kind, "⏳ Принял команду..."))

    def job():
        ok = POSTERS[kind]()
        _send(chat, (f"✅ Пост «{kind}» опубликован в {cfg.channel}."
                     if ok else "❌ Не получилось: нет подходящих игр или новостей."))
    _thread(job)


def _act_link(chat: int, text: str) -> bool:
    """Пост по ссылке на Steam или по AppID. True — сообщение было ссылкой."""
    m = _STORE_RE.search(text) or _ID_RE.match(text)
    if not m:
        return False
    left = _heavy_allowed()
    if left:
        _send(chat, f"🛑 Слишком часто. Подождите {int(left) + 1} с.")
        return True
    appid = int(m.group(1))
    _send(chat, f"⏳ Принял, собираю предпросмотр игры <code>{appid}</code>...")

    def job():
        p = build_game_post(appid)
        if not p:
            _send(chat, ("❌ Не нашёл игру. Проверьте ссылку: должно быть "
                         "<code>store.steampowered.com/app/1091500</code> "
                         "или просто <code>1091500</code>."))
            return
        _send_preview(chat, p, "game")
    _thread(job)
    return True


def _act_custom(chat: int) -> None:
    with _AWAIT_LOCK:
        _AWAIT[chat] = "custom"
    _send(chat, "✍️ Пришлите фото (можно с подписью) — покажу превью. "
                "Отмена: /cancel")


def _act_sync(chat: int) -> None:
    """Обновление каталога скидок с антифлудом (бьёт по Steam API)."""
    left = _heavy_allowed()
    if left:
        _send(chat, f"🛑 Слишком часто. Подождите {int(left) + 1} с.")
        return
    _send(chat, "⏳ Принял, обновляю каталог скидок...")

    def job():
        try:
            n = sync()
            _send(chat, f"✅ Синхронизировано: {n} игр со скидками")
        except Exception:  # noqa
            log.exception("sync job failed")
            _send(chat, "❌ Синхронизация не удалась.")
    _thread(job)


def _act_photo(chat: int, msg: dict) -> None:
    """Фото от владельца -> превью своего поста."""
    photos = msg.get("photo") or []
    if not photos:
        return
    left = _heavy_allowed()
    if left:
        _send(chat, f"🛑 Слишком часто. Подождите {int(left) + 1} с.")
        return
    with _AWAIT_LOCK:
        _AWAIT.pop(chat, None)
    biggest = max(photos, key=lambda p: int(p.get("file_size") or 0))
    # Bot API отдаёт файлы до 20 МБ; Telegram и так не пропустит больше,
    # но проверяем явно, чтобы не тянуть гигантский файл в память.
    if int(biggest.get("file_size") or 0) > 20 * 1024 * 1024:
        _send(chat, "❌ Фото слишком большое (максимум 20 МБ).")
        return
    caption = msg.get("caption") or ""
    _send(chat, "⏳ Принял фото, готовлю превью...")

    def job():
        path = download_telegram_file(biggest["file_id"])
        if not path:
            _send(chat, "❌ Не удалось скачать фото из Telegram.")
            return
        p = build_custom_post(caption, path)
        if not p:
            _send(chat, "❌ Пустой текст. Пришлите фото с подписью.")
            try:
                os.remove(path)
            except OSError:
                pass
            return
        _send_preview(chat, p, "custom")
    _thread(job)


def _clear_buttons(chat: int, message_id: int, text: str = "") -> None:
    if not (chat and message_id):
        return
    payload = {"chat_id": chat, "message_id": message_id,
               "reply_markup": json.dumps({"inline_keyboard": []})}
    _tg("editMessageReplyMarkup", json=payload)
    if text:
        _send(chat, text)


def _handle_callback(cb: dict) -> None:
    data = cb.get("data") or ""
    cq = cb.get("id")
    msg = cb.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    uid = (cb.get("from") or {}).get("id")

    action, _, token = data.partition(":")

    # СНАЧАЛА проверяем владельца и только потом трогаем _PENDING:
    # иначе посторонний, нажав кнопку, мог бы «сжечь» превью владельца.
    if uid != _owner_id():
        # answerCallbackQuery обязателен, иначе у нажавшего крутится спиннер,
        # но отвечаем один раз в сутки — флуд кнопками не должен генерировать
        # поток ответов от бота.
        txt = "⚠️ Нет доступа"
        if _same_recently(f"cbstranger:{uid}", 86400):
            txt = ""
        _tg("answerCallbackQuery", json={"callback_query_id": cq, "text": txt,
                                         "show_alert": bool(txt)})
        return

    p = _PENDING.pop(token, None)

    if action == "can" or not p:
        _tg("answerCallbackQuery", json={"callback_query_id": cq, "text": "Отменено"})
        if p:
            card = p["data"].get("card")
            if card and os.path.exists(card):
                try:
                    os.remove(card)
                except OSError:
                    pass
        _clear_buttons(chat, mid, "❌ Публикация отменена.")
        return

    _tg("answerCallbackQuery", json={"callback_query_id": cq,
                                     "text": "🚀 Публикую в канал..."})

    def job():
        if p["type"] == "game":
            ok = publish_game_preview(p["data"])
        else:
            ok = publish_custom_preview(p["data"])
        _clear_buttons(chat, mid,
                       f"✅ Опубликовано в {cfg.channel}." if ok
                       else "❌ Не удалось опубликовать — смотрите logs/bot.log")
    _thread(job)


def _handle_text(chat: int, uid: int, text: str, msg: dict) -> None:
    low = text.lower()
    owner = _owner_id()

    if low in ("/cancel", "отмена"):
        with _AWAIT_LOCK:
            _AWAIT.pop(chat, None)
        _send(chat, "Ок, отменил ожидание ввода.")
        return

    if uid != owner:
        # НЕ отвечаем посторонним: иначе флуд в бота превращается во флуд
        # из бота — тратится лимит Telegram (30 сообщений/с) и CPU.
        # Человеку, который ошибся адресом, достаточно одного ответа в сутки.
        if not _same_recently(f"stranger:{uid}", 86400):
            log.warning("Отклонено сообщение от постороннего ID %s", uid)
            _send(chat, ("⚠️ Это частный бот. Доступ закрыт.\n"
                         f"Ваш Telegram ID: <code>{uid}</code>"))
        return

    # кнопки меню
    if text == BTN_SINGLE:
        return _act_kind(chat, "single")
    if text == BTN_MULTI:
        return _act_kind(chat, "multi")
    if text == BTN_NEWS:
        return _act_kind(chat, "news")
    if text == BTN_LINK:
        with _AWAIT_LOCK:
            _AWAIT[chat] = "link"
        _send(chat, "🎮 Пришлите ссылку на игру в Steam или её AppID:\n"
                    "<code>https://store.steampowered.com/app/1091500</code>\n"
                    "<code>1091500</code>")
        return
    if text == BTN_CUSTOM:
        return _act_custom(chat)
    if text == BTN_STATUS:
        _send(chat, _status())
        _thread(lambda: _send(chat, _check_reply()))
        return
    if text == BTN_SYNC:
        return _act_sync(chat)
    if text == BTN_EMOJI:
        _send(chat, _emojis_reply())
        return

    # ожидание ссылки
    with _AWAIT_LOCK:
        awaiting = _AWAIT.get(chat)
    if awaiting == "link":
        with _AWAIT_LOCK:
            _AWAIT.pop(chat, None)
        if _act_link(chat, text):
            return
        _send(chat, "❌ Это не похоже на ссылку Steam или AppID.")

    # сама по себе ссылка/ID тоже работает
    if _act_link(chat, text):
        return

    # команды
    if low.startswith("/status"):
        _send(chat, _status())
        return
    if low.startswith("/check"):
        _send(chat, "⏳ Запускаю самодиагностику...")
        _thread(lambda: _send(chat, _check_reply()))
        return
    if low == "/sync" or low.startswith("/sync "):
        return _act_sync(chat)
    if low.startswith("/emojis"):
        _send(chat, _emojis_reply())
        return
    if low.startswith("/setemoji"):
        _send(chat, _set_emoji_cmd(msg, text))
        return
    if low.startswith("/resetemojis"):
        n = templates.reset_emojis()
        _send(chat, f"✅ Сброшено слотов: {n}. Вернулся набор по умолчанию.")
        return
    if low.startswith("/emoji"):
        _send(chat, _emoji_ids(msg))
        return
    if low.startswith("/post"):
        parts = low.split()
        kind = parts[1] if len(parts) > 1 else "single"
        if kind not in POSTERS:
            _send(chat, "Форматы: single | multi | news")
            return
        return _act_kind(chat, kind)
    if low.startswith(("/start", "/help")):
        _send(chat, HELP_TEXT)
        return

    # premium-эмодзи владельца
    ids = _custom_emoji_ids(msg)
    if ids:
        _send(chat, ("✨ Поймал premium-эмодзи.\nСохраните его в слот:\n"
                     "<code>/setemoji fire </code>" + text[:8] +
                     "\n\nСлоты: " + ", ".join(templates.EMOJI_SLOTS)))
        return

    # любое другое сообщение -> меню + подсказка
    _send(chat, "Выберите действие кнопками ниже 👇\n\n" + HELP_TEXT)


def process_update(u: dict):
    """Обрабатывает один апдейт Telegram (кнопки, команды, посты, модерация)."""
    cb = u.get("callback_query")
    if cb:
        _handle_callback(cb)
        return

    msg = u.get("message") or u.get("edited_message")
    if not msg:
        return
    chat_obj = msg.get("chat") or {}
    # 1) модерация чата обсуждений (группы комментов)
    if chat_obj.get("type") in ("group", "supergroup"):
        moderator.handle_update(u)
        return
    # 2) команды — только в личке
    if chat_obj.get("type") != "private":
        return
    uid = msg["from"]["id"]
    chat = msg["chat"]["id"]

    # первый написавший становится владельцем, если OWNER_ID не задан
    _claim_owner(uid, chat)

    # 3) фото владельца -> свой пост (даже без команды)
    if msg.get("photo"):
        if uid != _owner_id():
            if not _same_recently(f"stranger:{uid}", 86400):
                log.warning("Отклонено фото от постороннего ID %s", uid)
            return
        return _act_photo(chat, msg)

    text = (msg.get("text") or "").strip()
    if not text:
        return
    _handle_text(chat, uid, text, msg)


def process_pending_updates_once() -> int:
    """Одноразово забирает накопившиеся апдейты (getUpdates, без long-poll).

    Используется в режиме --once: отвечает владельцу на команды, модерит
    комментарии. Offset хранится в SQLite, поэтому апдейты не теряются и
    не обрабатываются дважды между запусками.
    """
    if NO_COMMANDS:
        log.info("NO_COMMANDS=1 — апдейты не забираю (слушает локальный бот)")
        return 0
    _drop_webhook()
    st = _storage()
    offset = int(st.kv_get("tg_offset") or 0)
    updates = _tg("getUpdates", json={"offset": offset, "timeout": 0}) or []
    n = 0
    for u in updates:
        offset = u["update_id"] + 1
        try:
            process_update(u)
        except Exception:  # noqa
            log.exception("Ошибка обработки апдейта %s", u.get("update_id"))
        n += 1
    if updates:
        st.kv_set("tg_offset", str(offset))
    return n


def _same_recently(text: str, window: float = 120.0) -> bool:
    """True, если точно такое же сообщение уже писалось в лог недавно.

    Нужно, чтобы длительный сбой Telegram (409 или 5xx) не превращал
    logs/bot.log в бесконечную ленту одинаковых строк.
    """
    key = text[:80]
    now = time.time()
    if now - _LAST_MSG.get(key, 0) < window:
        return True
    _LAST_MSG[key] = now
    return False


def command_handler():
    """Бесконечный приём апдейтов (long-poll) — только для локального бота."""
    if NO_COMMANDS:
        log.info("NO_COMMANDS=1 — режим публикации без прослушивания апдейтов")
        return
    # Telegram может отдавать 502/таймауты — пробуем несколько раз, но
    # НЕ блокируем запуск: важно, чтобы потоки поднялись как можно раньше.
    me = None
    for attempt in range(3):
        me = _tg("getMe")
        if me:
            break
        log.warning("getMe не сработал (попытка %d/3) — пробую снова", attempt + 1)
        time.sleep(2)
    if me:
        log.info("Бот: @%s", me.get("username"))
    _drop_webhook()
    st = _storage()
    offset = int(st.kv_get("tg_offset") or 0)
    poll = {"timeout": 15}
    # Пауза между попытками. Растёт при ошибках (в т.ч. 409 Conflict),
    # но мгновенно сбрасывается, как только апдейты снова приходят.
    # Так бот не «долбит» Telegram, если рядом поднялся второй экземпляр.
    pause = 0.0
    while True:
        if pause:
            time.sleep(pause)
        try:
            r = requests.post(API.format(token=cfg.bot_token, method="getUpdates"),
                              json=dict(poll, offset=offset), timeout=60)
            data = r.json()
        except requests.RequestException as e:
            pause = min(pause * 2 or 5, 60)
            msg = security.mask(str(e))
            if not _same_recently(msg):
                log.warning("getUpdates: сеть недоступна (%s) — повтор через %.0f с",
                            msg, pause)
            continue

        if not data.get("ok"):
            err = data.get("error_code")
            desc = str(data.get("description") or "")
            if err == 409:
                # Другой процесс держит long-poll (например, облачное окно).
                # Ждём и пробуем снова — владелец не должен видеть поток ошибок.
                pause = min(pause * 2 or 10, 60)
                if pause < 30 or not _same_recently(desc):
                    log.warning("getUpdates 409: апдейты забирает другой экземпляр — "
                                "повтор через %.0f с", pause)
            else:
                pause = min(pause * 2 or 3, 60)
                # 5xx — сбой на стороне Telegram: это временно, не засоряем лог
                if err and 500 <= int(err) < 600:
                    log.warning("Telegram недоступен (%s %s) — повтор через %.0f с",
                                err, desc, pause)
                else:
                    log.error("getUpdates failed: %s", security.mask(data))
            continue

        pause = 0.0
        updates = data.get("result") or []
        if not updates:
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                process_update(u)
            except Exception:  # noqa
                log.exception("Ошибка обработки апдейта %s", u.get("update_id"))
        st.kv_set("tg_offset", str(offset))


ADMIN_COMMANDS = ("/status", "/sync", "/post", "/emoji", "/emojis", "/setemoji",
                  "/resetemojis", "/check", "/start", "/help")

HELP_TEXT = (
    "<b>Быстрые кнопки</b> — под полем ввода 👇\n"
    "🎯 1 игра · 📦 4 игры · 📰 новость · 🎮 по ссылке/ID\n"
    "✍️ свой пост · 📊 статус · 🎨 эмодзи · 🔄 sync\n"
    "\n<b>Команды</b>\n"
    "/status — статистика и расписание\n"
    "/check — самодиагностика автономности\n"
    "/sync — обновить каталог скидок\n"
    "/post single — пост об одной игре с описанием\n"
    "/post multi — подборка из 4 игр\n"
    "/post news — игровая новость дня\n"
    "\n<b>Ручные посты</b>\n"
    "Пришлите ссылку <code>store.steampowered.com/app/1091500</code>\n"
    "или AppID <code>1091500</code> — покажу превью с кнопкой «Опубликовать».\n"
    "Пришлите фото — соберу свой пост с подписью канала.\n"
    "\n<b>Эмодзи постов</b>\n"
    "/emojis — какие эмодзи стоят в каждом слоте\n"
    "/setemoji fire 🔥 — запомнить эмодзи для слота (можно premium)\n"
    "/resetemojis — вернуть набор по умолчанию\n"
    "/emoji_id — переслать premium-эмодзи и узнать его emoji-id\n"
    "\n<b>Расписание</b>: 12:00 подборка · 15:00 игра · 18:00 новость · 21:00 игра"
)


def _esc(s) -> str:
    return html.escape(str(s), quote=False)


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Подстрока по смещениям UTF-16 (так их считает Telegram)."""
    data = text.encode("utf-16-le", "surrogatepass")
    chunk = data[offset * 2:(offset + length) * 2]
    return chunk.decode("utf-16-le", "replace")


def _custom_emoji_ids(msg: dict) -> list[str]:
    ents = list(msg.get("entities") or []) + list(msg.get("caption_entities") or [])
    return [e["custom_emoji_id"] for e in ents
            if e.get("type") == "custom_emoji" and e.get("custom_emoji_id")]


def _emojis_reply() -> str:
    """Показывает текущие эмодзи каждого слота."""
    lines = ["<b>✨ Эмодзи постов</b>", ""]
    for slot, cur, dflt in templates.emoji_table():
        changed = (cur["char"], cur["id"]) != (dflt["char"], dflt["id"])
        mark = "✏️" if changed else "▫️"
        hint = templates.SLOT_HINTS.get(slot, "")
        lines.append(f"{mark} <code>{slot}</code> {templates.emo(slot)} — <i>{_esc(hint)}</i>")
    lines += [
        "",
        "Сменить: <code>/setemoji fire 🔥</code>",
        "Premium: пришлите эмодзи вместе с командой — сохраню его id.",
        "Сбросить: /resetemojis",
    ]
    return "\n".join(lines)


def _set_emoji_cmd(msg: dict, text: str) -> str:
    """/setemoji <слот> <эмодзи> — запоминает эмодзи навсегда (в т.ч. premium)."""
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        return ("Формат: <code>/setemoji fire 🔥</code>\n"
                "Слоты: " + ", ".join(templates.EMOJI_SLOTS))
    slot = parts[1].strip().lower()
    if slot not in templates.EMOJI_SLOTS:
        return (f"❌ Нет слота <code>{_esc(slot)}</code>.\n"
                "Доступно: " + ", ".join(templates.EMOJI_SLOTS))

    # offset эмодзи-аргумента в UTF-16: «/setemoji» + пробел + слот + пробел
    prefix = f"{parts[0]} {parts[1]} "
    prefix_u16 = len(prefix.encode("utf-16-le", "surrogatepass")) // 2

    char, eid = "", ""
    ents = list(msg.get("entities") or [])
    for e in ents:
        if e.get("type") != "custom_emoji":
            continue
        off = int(e.get("offset") or 0)
        if off < prefix_u16:
            continue
        char = _utf16_slice(text, off, int(e.get("length") or 0)).strip()
        eid = str(e.get("custom_emoji_id") or "")
        break
    if not char:
        char = parts[2].strip()

    if not char:
        return "❌ Не вижу эмодзи. Пример: <code>/setemoji fire 🔥</code>"
    if not templates.set_emoji(slot, char, eid):
        return "❌ Не получилось сохранить эмодзи."

    kind = "premium-эмодзи" if eid else "эмодзи"
    return (f"✅ Слот <code>{slot}</code> теперь {templates.emo(slot)} ({kind}).\n"
            f"Подставлю его во все новые посты.")


def _check_reply() -> str:
    """Форматирует результат selfcheck для Telegram."""
    from poster import selfcheck

    checks = selfcheck()
    lines = ["<b>🩺 Самодиагностика</b>", ""]
    for c in checks:
        lines.append(f"{'✅' if c['ok'] else '❌'} <b>{_esc(c['name'])}</b> — {_esc(c['detail'])}")
    bad = sum(1 for c in checks if not c["ok"])
    lines += ["", "Всё в порядке ✅" if not bad else f"Требует внимания: {bad} ❌"]
    return "\n".join(lines)


def _emoji_ids(msg: dict) -> str:
    """Возвращает custom_emoji_id из пересланного сообщения с premium-эмодзи."""
    ents = list(msg.get("entities") or []) + list(msg.get("caption_entities") or [])
    ids = [e.get("custom_emoji_id") for e in ents if e.get("type") == "custom_emoji"]
    ids = [i for i in ids if i]
    if not ids:
        return ("ℹ️ Перешлите боту сообщение с Telegram Premium эмодзи — "
                "я пришлю его <code>custom_emoji_id</code>.")
    uniq = list(dict.fromkeys(ids))
    body = "\n".join(f"<code>{i}</code>" for i in uniq)
    return (
        "✨ Найдены premium emoji:\n" + body +
        "\n\nДобавьте в <code>.env</code>:\n"
        f"<code>CUSTOM_EMOJI_MAP=fire={uniq[0]}</code>"
    )


def main():
    args = sys.argv[1:]
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args and args[0] == "--selfcheck":
        raise SystemExit(run_selfcheck())
    if args and args[0] == "--once":
        raise SystemExit(run_once())
    if args and args[0] == "--listen":
        # serverless-режим: слушаем личку (команды) N секунд и выходим.
        # Используется в GitHub Actions между постами по расписанию.
        secs = int(args[1]) if len(args) > 1 and args[1].isdigit() else 240
        cfg.ensure_dirs()
        import threading
        threading.Thread(target=command_handler, daemon=True).start()
        log.info("Слушаю личку %d с ...", secs)
        time.sleep(secs)
        raise SystemExit(0)
    if args and args[0] == "--force" and len(args) > 1 and args[1] in POSTERS:
        cfg.ensure_dirs()
        raise SystemExit(run_once(force_kind=args[1]))

    if args and args[0] == "--serve":
        # окно работы бота (используется в GitHub Actions): публикует по слотам
        # и слушает команды N секунд, затем выходит. Следующее окно запускает
        # сам workflow — так бот работает круглосуточно без cron каждые 5 минут.
        secs = int(args[1]) if len(args) > 1 and args[1].isdigit() else 3600
        _start_daemon(secs)
        return

    _start_daemon()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=20).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, text=True, timeout=20)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        log.warning("Не удалось остановить процесс %s", pid)


def _acquire_lock() -> None:
    """Гарантирует единственный экземпляр бота (только один getUpdates)."""
    cfg.ensure_dirs()
    path = os.path.join(cfg.data_dir, "bot.lock")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = int((f.read() or "0").strip())
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            log.warning("Найден живой старый экземпляр (PID %s) — останавливаю", old)
            _kill_pid(old)
            time.sleep(2)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    log.info("PID %s записан в %s", os.getpid(), path)


def _setup_file_log() -> None:
    cfg.ensure_dirs()
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler):
            return
    h = RotatingFileHandler(os.path.join(cfg.logs_dir, "bot.log"),
                            maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    h.addFilter(security.TokenMaskFilter())  # токен не должен попасть в файл
    root.addHandler(h)


def _start_daemon(secs: int | None = None):
    cfg.ensure_dirs()
    _setup_file_log()
    if not cfg.bot_token:
        log.error("Нет BOT_TOKEN в .env — скопируй .env.example в .env и заполни")
        raise SystemExit(2)

    # единственный экземпляр: если старая копия висит — гасим, иначе Telegram
    # будет отдавать апдейты то одному, то другому процессу (409 Conflict)
    _acquire_lock()

    # НИКАКОГО блокирующего sync() перед потоками! Сначала сброс вебхука
    # (чтобы getUpdates не ловил 409), затем сразу потоки — команды владельца
    # отвечают мгновенно. Первичный sync делает scheduler_loop в фоне.
    _drop_webhook()
    if cfg.post_schedule:
        threading.Thread(target=scheduler_loop, daemon=True).start()
    else:
        log.info("POST_SCHEDULE=0 — публикую только вручную, расписание в облаке")
    threading.Thread(target=command_handler, daemon=True).start()

    log.info("Бот запущен%s. Канал: %s | расписание: %s",
             f" на {secs} с" if secs else " 24/7", cfg.channel,
             ", ".join(f"{s['time'][0]:02d}:{s['time'][1]:02d} {s['kind']}"
                       for s in cfg.schedule))

    # Владельцу — весточка, что бот снова на связи (режим 24/7, только раз)
    if not secs and cfg.owner_id:
        _notify_owner()
    if secs:
        time.sleep(secs)
        log.info("Окно работы закончилось — выходим, следующее запустит workflow")
        raise SystemExit(0)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Остановлен вручную (Ctrl+C)")


def _notify_owner() -> None:
    """Сообщает владельцу в личку, что бот запущен и слушает команды."""
    payload = {
        "text": ("✅ <b>Бот онлайн.</b>\n"
                 f"Канал: {cfg.channel}\n"
                 "Кнопки — под полем ввода. Пишите /start для справки."),
        "parse_mode": "HTML",
        "reply_markup": json.dumps(_menu(), ensure_ascii=False),
    }
    for attempt in range(3):
        r = _tg("sendMessage", json=dict(payload, chat_id=cfg.owner_id))
        if r:
            log.info("Владельцу отправлено уведомление «бот онлайн»")
            return
        log.warning("Не удалось уведомить владельца (попытка %d/3) — Telegram "
                    "может быть временно недоступен", attempt + 1)
        time.sleep(5)
    log.warning("Владелец не получил уведомление — проверьте, что он писал боту "
                "(OWNER_ID=%s)", cfg.owner_id)


if __name__ == "__main__":
    main()
