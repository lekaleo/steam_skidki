"""Модерация чата обсуждений канала: мат, оскорбления, спам-ссылки.

Политика: без перманентных банов — только временные муты по нарастающей.
1-е нарушение — 15 мин, 2-е — 2 часа, 3-е+ — 24 часа.

ДЕТЕКТОР (важно!):
- текст НЕ склеивается в кашу без пробелов — разбивается на отдельные токены;
- каждый токен проверяется шаблонами с привязкой к границам слова (^…$);
- для опасных корней (еб, сук, муд, хер, жоп) — белый список обычных слов,
  поэтому «тебе», «себе», «ребята», «требования», «мудрый», «рисунок»,
  «парикмахер», «барсук», «сукно», «учеба», «мебель» НИКОГДА не считаются матом.

Самопроверка: python moderator.py --selftest
"""
import html
import logging
import re
import threading
import time

import requests

from config import cfg
from storage import Storage

log = logging.getLogger("moderator")

API = "https://api.telegram.org/bot{token}/{method}"

# ---------------------------------------------------------------- нормализация

# обходы через похожие символы
LEET = str.maketrans({
    "@": "а", "0": "о", "1": "и", "3": "е", "4": "а", "$": "s", "6": "б",
    "ё": "е", "i": "и", "|": "",
})

# белый список: обычные слова, которые не должны триггерить фильтр
SAFE_WORDS = {
    "тебе", "себе", "себя", "собой", "ребята", "ребенок", "ребятам", "ребятами",
    "небо", "небе", "небеса", "небесный", "мебель", "мебели", "мебелью",
    "требовать", "требую", "требования", "требование", "требований", "потребовать",
    "употреблять", "теребить", "учеба", "учебный", "учебник", "учебники",
    "хлеб", "хлеба", "хлебом", "погреб", "погреба", "серебро", "серебряный",
    "гребень", "гребня", "гребля", "лебедь", "лебедя", "стебель", "жребий",
    "веб", "вебка", "вебсайт", "дебаты", "дебатов", "хештег", "хеш",
    "барсук", "барсука", "сукно", "сукна", "сукном", "парикмахер", "парикмахера",
    "страховка", "страховку", "страховой", "страхование",
    "мудрый", "мудрость", "мудрец", "мудро", "мудрее", "премудрый",
    "рисунок", "рисунки", "рисунка", "рисую", "рисует", "нарисовал",
    "полюс", "глобус", "автобус", "фикус", "уксус", "фокус", "искус",
}

# мат и обсценная лексика: применяется К ОДНОМУ токену, ^… = начало слова
MAT_PATTERNS = [re.compile(p) for p in (
    r"ху[йяеёиую]",          # хуй, хуя, хуе, нахуй…
    r"хуйл",                 # хуйло
    r"пизд",                 # пизда, пиздец, распиздяй
    r"^бля",                 # бля, блядь, блят
    r"залуп",
    r"дроч",
    r"шлюх",
    r"^муд(ак|ил|озвон|ень|ищ)",   # мудак, мудила (НО НЕ «мудрый»)
    r"^пидор|^пидар|^педик",
    r"^гандон|^гондон",
    r"^говн",
    r"^жоп",
    r"^мраз[ьи]",
    r"^твар[ьи]",
    r"^сук[аио]$|^суч[кае]|^сучар",   # сука, сучки (НО НЕ «барсук», «сукно»)
    r"^[её]б",               # ебать, ёбнуть, еблан
    r"^у[её]б",              # уёбок
    r"(за|на|от|съ|въ|вы|до|раз|пере|под|при|про|объ|изъ|по|ис|рас|недо|долбо|взъ)[её]б",
    r"^ублюд",
    r"^хер[ня]?$|^херов",    # хер, херня (НО НЕ «парикмахер»)
    r"^елд[аоы]",
    r"^манда",
    r"^срак",
)]

EN_PATTERNS = [re.compile(p) for p in (
    r"^fuck", r"^fuxk", r"^phuck", r"^shit", r"^bitch", r"^asshole",
    r"^bastard", r"^whore", r"^slut", r"^nigg", r"^retard", r"^cunt",
    r"^dick(head)?$", r"^pussy$",
)]

# прямые оскорбления участников (только явные, чтобы не ловить «тупая игра»)
INSULT_PATTERNS = [re.compile(p) for p in (
    r"^дурак", r"^идиот", r"^дебил", r"^кретин", r"^придурок",
    r"^дегенерат", r"^лошар", r"^чмо", r"^ничтожеств",
    r"^урод$|^уроды$|^уродина$|^уродка$",
)]

SPAM_WORDS = (
    "заработ", "крипт", "казино", "ставк", "инвест", "подписывайся", "подпишись",
    "промокод", "займ", "заработок", "free money", "airdrop", "forex", "бинарн",
    "сигнал", "развод",
)

_LINK_RE = re.compile(r"(https?://\S+|t\.me/\S+|@[a-zA-Z0-9_]{4,})", re.I)
_INVITE_RE = re.compile(r"t\.me/(\+|joinchat/)", re.I)


# ---------------------------------------------------------------- детектор

def _tokens(text: str) -> list[str]:
    """Разбивает текст на отдельные нормализованные слова (пробелы сохраняются!)."""
    t = (text or "").lower().translate(LEET)
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)   # пунктуация: «ху*й» -> «хуй»
    t = re.sub(r"\s+", " ", t).strip()
    out = []
    for w in t.split(" "):
        if not w:
            continue
        w = re.sub(r"(.)\1{2,}", r"\1\1", w)  # хууууй -> хууй
        w = re.sub(r"(.)\1", r"\1", w)        # хууй -> хуй
        out.append(w)
    return out


def _matches(text: str, patterns) -> bool:
    for tok in _tokens(text):
        if tok in SAFE_WORDS:
            continue
        if any(p.search(tok) for p in patterns):
            return True
    return False


def contains_profanity(text: str) -> bool:
    return _matches(text, MAT_PATTERNS) or _matches(text, EN_PATTERNS)


def contains_insult(text: str) -> bool:
    return _matches(text, INSULT_PATTERNS)


def is_spam(text: str) -> bool:
    if not text:
        return False
    if _INVITE_RE.search(text):
        return True
    links = _LINK_RE.findall(text)
    low = text.lower()
    has_spam_word = any(w in low for w in SPAM_WORDS)
    if len(links) >= 2:
        return True
    if links and has_spam_word:
        return True
    return False


# ---------------------------------------------------------------- действия

_storage: Storage | None = None


def _st() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage(cfg.db_path)
    return _storage


def _mask(s) -> str:
    return cfg.mask(s)


def _api(method: str, **kwargs) -> dict | None:
    try:
        r = requests.post(API.format(token=cfg.bot_token, method=method), timeout=30, **kwargs)
        data = r.json()
        if not data.get("ok"):
            log.warning("moderator %s: %s", method, _mask(data))
            return None
        return data.get("result")
    except requests.RequestException as e:
        log.warning("moderator %s error: %s", method, _mask(e))
        return None


def _mute_seconds(strikes: int) -> int:
    if strikes <= 1:
        return 15 * 60
    if strikes == 2:
        return 2 * 3600
    return 24 * 3600


def _mute_label(sec: int) -> str:
    if sec < 3600:
        return f"{sec // 60} мин"
    return f"{sec // 3600} ч"


def _delete_later(chat_id: int, message_id: int, delay: int = 30):
    def job():
        time.sleep(delay)
        _api("deleteMessage", json={"chat_id": chat_id, "message_id": message_id})

    threading.Thread(target=job, daemon=True).start()


def _restrict(chat_id: int, user_id: int, until_ts: int):
    _api("restrictChatMember", json={
        "chat_id": chat_id,
        "user_id": user_id,
        "permissions": {
            "can_send_messages": False,
            "can_send_audios": False,
            "can_send_documents": False,
            "can_send_photos": False,
            "can_send_videos": False,
            "can_send_video_notes": False,
            "can_send_voice_notes": False,
            "can_send_polls": False,
            "can_send_other_messages": False,
            "can_add_web_page_previews": False,
            "can_invite_users": False,
        },
        "until_date": until_ts,
    })


def handle_message(msg: dict) -> bool:
    """Обрабатывает одно сообщение из чата обсуждений. True если было нарушение."""
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if chat_type not in ("group", "supergroup"):
        return False
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return False
    # админов не трогаем
    member = _api("getChatMember", json={"chat_id": chat_id, "user_id": user.get("id")})
    if member and member.get("status") in ("administrator", "creator"):
        return False

    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        return False

    bad = contains_profanity(text)
    insult = contains_insult(text)
    spam = is_spam(text)
    if not (bad or insult or spam):
        return False

    uid = user.get("id")
    mid = msg.get("message_id")
    _api("deleteMessage", json={"chat_id": chat_id, "message_id": mid})
    strikes = _st().add_strike(uid, chat_id)
    sec = _mute_seconds(strikes)
    _restrict(chat_id, uid, int(time.time()) + sec)

    if bad:
        reason = "нецензурную лексику"
    elif insult:
        reason = "оскорбление участника"
    else:
        reason = "спам/рекламу"
    name = html.escape(user.get("first_name") or "участник")
    note = (
        f"<b>🛡 Комментарий удалён</b> за {reason}.\n"
        f"{name}, выдан временный режим тишины на <b>{_mute_label(sec)}</b>."
    )
    sent = _api("sendMessage", json={"chat_id": chat_id, "text": note, "parse_mode": "HTML"})
    if sent and sent.get("message_id"):
        _delete_later(chat_id, sent["message_id"], 30)
    log.info("Нарушение от %s в %s: strikes=%d, mute=%s", uid, chat_id, strikes, _mute_label(sec))
    return True


def handle_update(update: dict) -> bool:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return False
    try:
        return handle_message(msg)
    except Exception as e:  # noqa
        log.warning("moderator handle error: %s", _mask(e))
        return False


# ---------------------------------------------------------------- самопроверка

CLEAN_PHRASES = [
    "Ребята, эта игра просто огонь, рекомендую!",
    "Тебе стоит попробовать, себе тоже куплю на распродаже",
    "Какие требования к системе?",
    "Учеба мешает играть, но скидки не ждут",
    "Мудрый ход от разработчиков — снизили цену",
    "Красивый рисунок на обложке игры",
    "Барсук в этой игре милый",
    "Сукно для костюма персонажа",
    "Парикмахер в симуляторе — это забавно",
    "Страховка на машину дорогая",
    "Небо в игре просто красивое",
    "Мебель можно расставить в доме",
    "Хлеб в игре можно печь",
    "Погреб в подвале нашёл",
    "Серебро и золото в инвентаре",
    "Лебедь на озере плавает",
    "Купил себе скидку, тебе тоже советую",
    "Веб-версия магазина работает",
    "Отличная скидка, беру!",
    "Обновление вышло, игра стала лучше",
]

BAD_PHRASES = [
    "ты хуй",
    "это пиздец какой-то",
    "блядь, опять скидка мимо",
    "заебал уже этот магазин",
    "ебать копать, дорого",
    "ты мудак",
    "сука, как дорого",
    "иди нахуй",
    "пидор",
    "говно игра",
    "ты дебил",
    "fuck this game",
]


def _selftest() -> int:
    fails = 0
    print("=== ОБЫЧНЫЕ ФРАЗЫ (ожидается 0 срабатываний) ===")
    for p in CLEAN_PHRASES:
        bad = contains_profanity(p) or contains_insult(p) or is_spam(p)
        if bad:
            fails += 1
            print(f"  ✗ ЛОЖНОЕ СРАБАТЫВАНИЕ: {p!r} -> tokens={_tokens(p)}")
    print(f"  Проверено {len(CLEAN_PHRASES)}, ложных срабатываний: {fails}")

    print("\n=== МАТ (ожидается 100% детект) ===")
    missed = 0
    for p in BAD_PHRASES:
        bad = contains_profanity(p) or contains_insult(p)
        if not bad:
            missed += 1
            print(f"  ✗ ПРОПУЩЕНО: {p!r}")
    print(f"  Проверено {len(BAD_PHRASES)}, пропущено: {missed}")

    total = fails + missed
    print("\nИТОГ:", "✅ ВСЁ ЧИСТО" if total == 0 else f"❌ проблем: {total}")
    return total


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if "--selftest" in sys.argv:
        raise SystemExit(1 if _selftest() else 0)
    print(__doc__)
