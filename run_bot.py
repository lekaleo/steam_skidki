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
import logging
import sys
import time
from datetime import datetime

import security
import templates
from config import cfg
from poster import POSTERS, sync, _storage, _tg, maybe_cleanup
import moderator

security.install()

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
security.install()  # повесить фильтр и на хендлеры basicConfig

WINDOW_H = 3.5  # сколько часов после слота ещё считается «сейчас»


def now() -> datetime:
    return datetime.now(cfg.tz)


def _slot_key(hh, mm, day) -> str:
    return f"done:{day}:{hh:02d}:{mm:02d}"


def sync_if_needed(force=False) -> int:
    st = _storage()
    last = st.kv_get_json("last_sync") or {}
    last_ts = last.get("last_sync", 0)
    if force or time.time() - last_ts > cfg.sync_hours * 3600:
        return sync()
    log.info("Sync не нужен (последний %s)", datetime.fromtimestamp(last_ts, cfg.tz).strftime("%d.%m %H:%M") if last_ts else "никогда")
    return 0


def run_once(force_kind: str | None = None) -> int:
    """Одноразовый режим для serverless-cron."""
    cfg.ensure_dirs()
    maybe_cleanup()   # раз в сутки: не даём папке images/ расти бесконечно
    sync_if_needed()
    st = _storage()

    if force_kind:
        ok = POSTERS[force_kind]()
        log.info("Принудительный пост %s: %s", force_kind, ok)
        return 0 if ok else 1

    due = _schedule_due()
    if not due:
        log.info("Сейчас нет слота для поста — выходим")
        return 0

    hh, mm, kind = due[0]
    log.info("Слот %02d:%02d тянет пост: %s", hh, mm, kind)
    ok = POSTERS[kind]()
    if ok:
        st.kv_set(_slot_key(hh, mm, now().strftime("%Y-%m-%d")), "1")
        log.info("✅ Опубликовано")
        return 0
    log.warning("❌ Не удалось опубликовать %s", kind)
    return 1


def _schedule_due(last: dict | None = None) -> list[tuple[int, int, str]]:
    """Слоты, которые уже наступили и ещё НЕ отработали сегодня.

    Возвращает (hh, mm, kind) — каждый слот считается отдельно по своему времени.
    Факт публикации хранится в kv как done:YYYY-MM-DD:HH:MM, поэтому даже
    перезапуск процесса не приведёт к повторному посту того же слота.
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
        if (cur - slot_dt).total_seconds() > WINDOW_H * 3600:
            continue
        if st.kv_get(_slot_key(hh, mm, day)) == "1":
            continue
        if last and last.get(f"{hh:02d}:{mm:02d}") == day:
            continue
        due.append((hh, mm, kind))
    return due


def scheduler_loop():
    last: dict = {}
    last_sync = 0.0
    while True:
        try:
            if cfg.sync_hours and time.time() - last_sync > cfg.sync_hours * 3600:
                sync()
                last_sync = time.time()

            st = _storage()
            for hh, mm, kind in _schedule_due(last):
                key = f"{hh:02d}:{mm:02d}"
                day = now().strftime("%Y-%m-%d")
                log.info("Сработал слот %s (%s)", key, kind)
                try:
                    ok = POSTERS[kind]()
                except Exception as e:  # noqa
                    log.exception("Ошибка в посте %s: %s", kind, e)
                    ok = False
                if ok:
                    st.kv_set(_slot_key(hh, mm, day), "1")
                    last[key] = day
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
    """Сброс вебхука: без него getUpdates не получает сообщения (409 Conflict)."""
    try:
        _tg("deleteWebhook", json={"drop_pending_updates": False})
        info = _tg("getWebhookInfo") or {}
        if info.get("url"):
            log.warning("Вебхук всё ещё установлен: %s", info.get("url"))
        else:
            log.info("Вебхук сброшен — работаем через getUpdates")
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


def command_handler():
    me = _tg("getMe")
    if me:
        log.info("Бот: @%s", me.get("username"))
    _drop_webhook()
    offset = 0
    while True:
        try:
            updates = _tg("getUpdates", json={"offset": offset, "timeout": 25})
            if not updates:
                time.sleep(2)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue
                chat_obj = msg.get("chat") or {}
                # 1) модерация чата обсуждений (группы комментов)
                if chat_obj.get("type") in ("group", "supergroup"):
                    moderator.handle_update(u)
                    continue
                # 2) команды — только в личке
                if chat_obj.get("type") != "private":
                    continue
                uid = msg["from"]["id"]
                chat = msg["chat"]["id"]
                text = (msg.get("text") or "").strip()
                low = text.lower()

                # первый написавший становится владельцем, если OWNER_ID не задан
                _claim_owner(uid, chat)
                owner = _owner_id()
                is_admin_cmd = low.startswith(ADMIN_COMMANDS)

                if is_admin_cmd and uid != owner:
                    _tg("sendMessage", json={
                        "chat_id": chat,
                        "text": ("⚠️ Доступ закрыт. Укажите ваш Telegram ID "
                                 f"({uid}) в переменной OWNER_ID в файле .env"),
                    })
                    continue

                reply = None
                if low.startswith("/status"):
                    reply = _status()
                elif low.startswith("/check"):
                    reply = _check_reply()
                elif low == "/sync":
                    n = sync()
                    reply = f"✅ Синхронизировано: {n} игр со скидками"
                elif low.startswith("/emojis"):
                    reply = _emojis_reply()
                elif low.startswith("/setemoji"):
                    reply = _set_emoji_cmd(msg, text)
                elif low.startswith("/resetemojis"):
                    n = templates.reset_emojis()
                    reply = f"✅ Сброшено слотов: {n}. Вернулся стильный набор по умолчанию."
                elif low.startswith("/emoji"):
                    reply = _emoji_ids(msg)
                elif low.startswith("/post"):
                    parts = low.split()
                    kind = parts[1] if len(parts) > 1 else "single"
                    if kind in POSTERS:
                        ok = POSTERS[kind]()
                        reply = f"✅ Пост {kind} опубликован" if ok else "❌ Не получилось (нет кандидатов?)"
                    else:
                        reply = "Форматы: " + " | ".join(POSTERS)
                elif low.startswith(("/start", "/help")):
                    reply = HELP_TEXT
                if reply and chat:
                    _tg("sendMessage", json={"chat_id": chat, "text": reply, "parse_mode": "HTML"})
                    continue

                # 3) владелец просто прислал premium-эмодзи — подсказываем слоты
                ids = _custom_emoji_ids(msg)
                if ids and owner and uid == owner:
                    _tg("sendMessage", json={
                        "chat_id": chat,
                        "parse_mode": "HTML",
                        "text": ("✨ Поймал premium-эмодзи.\n"
                                 "Сохраните его в слот:\n"
                                 "<code>/setemoji fire </code>" + msg.get("text", "")[:8] +
                                 "\n\nСлоты: " + ", ".join(templates.EMOJI_SLOTS)),
                    })
        except Exception:  # noqa
            log.exception("Ошибка в обработчике команд")
            time.sleep(5)


ADMIN_COMMANDS = ("/status", "/sync", "/post", "/emoji", "/emojis", "/setemoji",
                  "/resetemojis", "/check", "/start", "/help")

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/status — статистика и расписание\n"
    "/check — самодиагностика автономности\n"
    "/sync — обновить каталог скидок\n"
    "/post single — пост об одной игре с описанием\n"
    "/post multi — подборка из 4 игр\n"
    "/post news — игровая новость дня\n"
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


def _start_daemon(secs: int | None = None):
    cfg.ensure_dirs()
    if not cfg.bot_token:
        log.error("Нет BOT_TOKEN в .env — скопируй .env.example в .env и заполни")
        raise SystemExit(2)

    try:
        sync()
    except Exception:  # noqa
        log.exception("Первичный sync не удался")

    import threading
    t1 = threading.Thread(target=scheduler_loop, daemon=True)
    t2 = threading.Thread(target=command_handler, daemon=True)
    t1.start()
    t2.start()
    log.info("Бот запущен%s. Канал: %s | расписание: %s",
             f" на {secs} с" if secs else " 24/7", cfg.channel,
             ", ".join(f"{s['time'][0]:02d}:{s['time'][1]:02d} {s['kind']}" for s in cfg.schedule))
    if secs:
        time.sleep(secs)
        log.info("Окно работы закончилось — выходим, следующее запустит workflow")
        raise SystemExit(0)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
