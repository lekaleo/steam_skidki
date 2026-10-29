"""Календарь праздников и сезонных распродаж Steam.

Модуль не ходит в сеть: только текущая дата -> активное событие -> плашка на
карточке, тематические ключевые слова и бонус к весу игры.

Даты заданы «месяц-день» и не привязаны к году, поэтому календарь работает
бесконечно (в том числе на стыке года: 15 декабря – 10 января).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from config import cfg


@dataclass(frozen=True)
class Event:
    key: str                 # машинное имя = kind карточки (KIND_LABEL в images.py)
    label: str               # плашка на карточке
    title: str               # заголовок поста
    emoji: str               # слот эмодзи в templates.py
    tagline: str             # короткая подпись под заголовком
    keywords: tuple[str, ...] = ()   # что делает игру тематической
    min_discount: int = 0    # с какой скидки берём игры в тематическую подборку
    boost: int = 0           # бонус к весу игры (только порядок очереди!)


# ---------------------------------------------------------------- события

HALLOWEEN = Event(
    key="halloween",
    label="ХЭЛЛОУИН В STEAM",
    title="ХЭЛЛОУИН В STEAM",
    emoji="halloween",
    tagline="Хорроры, выживание и мрачные истории со скидками",
    keywords=(
        "horror", "хоррор", "ужас", "zombie", "зомби", "survival", "выживание",
        "dark", "мрачн", "мистик", "mystic", "триллер", "thriller", "вампир",
        "vampire", "resident evil", "silent hill", "dead", "fear", "страх",
        "phasmophobia", "outlast", "ghost", "призрак", "демон", "witch", "ведьм",
        "evil", "зло", "проклят", "curse", "маньяк", "slasher", "крипи", "creepy",
    ),
    min_discount=30,
    boost=160,
)

AUTUMN = Event(
    key="autumn",
    label="ОСЕННЯЯ РАСПРОДАЖА",
    title="ОСЕННЯЯ РАСПРОДАЖА STEAM",
    emoji="fire",
    tagline="Главные хиты сезона — от 50%",
    keywords=(),
    min_discount=50,
    boost=0,
)

WINTER = Event(
    key="winter",
    label="ЗИМНЯЯ РАСПРОДАЖА",
    title="ЗИМНЯЯ РАСПРОДАЖА STEAM",
    emoji="newyear",
    tagline="Уютные игры, кооперативы и главные блокбастеры года",
    keywords=(
        "christmas", "рождеств", "новый год", "новогодн", "winter", "зима", "зимн",
        "snow", "снег", "cozy", "уют", "co-op", "coop", "кооператив", "вместе",
        "holiday", "праздник", "santa", "санта", "frost", "мороз",
    ),
    min_discount=40,
    boost=120,
)

SPRING = Event(
    key="spring",
    label="ВЕСЕННЯЯ РАСПРОДАЖА",
    title="ВЕСЕННЯЯ РАСПРОДАЖА STEAM",
    emoji="fire",
    tagline="Свежие скидки к началу сезона",
    keywords=(),
    min_discount=40,
    boost=0,
)

SUMMER = Event(
    key="summer",
    label="ЛЕТНЯЯ РАСПРОДАЖА",
    title="ЛЕТНЯЯ РАСПРОДАЖА STEAM",
    emoji="fire",
    tagline="Главная распродажа года — берите, пока дают",
    keywords=(),
    min_discount=40,
    boost=0,
)

# Порядок важен: если окна пересеклись, выигрывает событие выше в списке.
# (month, day) — начало и конец включительно; конец < начала = переход через год.
WINDOWS: tuple[tuple[Event, tuple[int, int], tuple[int, int]], ...] = (
    (WINTER, (12, 15), (1, 10)),
    (HALLOWEEN, (10, 20), (11, 5)),
    (AUTUMN, (11, 24), (12, 4)),
    (SPRING, (3, 13), (3, 20)),
    (SUMMER, (6, 25), (7, 11)),
)

ALL_EVENTS = (HALLOWEEN, AUTUMN, WINTER, SPRING, SUMMER)


# ---------------------------------------------------------------- выбор события

def _md(d: date) -> tuple[int, int]:
    return (d.month, d.day)


def _in_window(d: date, start: tuple[int, int], end: tuple[int, int]) -> bool:
    cur = _md(d)
    if start <= end:
        return start <= cur <= end
    # окно через Новый год: 15.12 -> 10.01
    return cur >= start or cur <= end


def today() -> date:
    return datetime.now(cfg.tz).date()


def active_event(now: datetime | date | None = None) -> Event | None:
    """Активное сезонное событие или None, если сейчас обычные дни."""
    d = _as_date(now)
    for event, start, end in WINDOWS:
        if _in_window(d, start, end):
            return event
    return None


def _as_date(now: datetime | date | None) -> date:
    if now is None:
        return today()
    if isinstance(now, datetime):
        return now.astimezone(cfg.tz).date() if now.tzinfo else now.date()
    return now


def next_event(now: datetime | date | None = None) -> tuple[Event, date] | None:
    """Ближайшее будущее событие и дата его старта (для /check и заголовков)."""
    d = _as_date(now)
    current = active_event(d)
    for i in range(1, 400):
        probe = d + timedelta(days=i)
        ev = active_event(probe)
        if ev is not None and ev != current:
            return ev, probe
    return None


def days_left(now: datetime | date | None = None) -> int:
    """Сколько дней осталось до конца активного события (0, если события нет)."""
    d = _as_date(now)
    for event, start, end in WINDOWS:
        if not _in_window(d, start, end):
            continue
        for i in range(0, 400):
            probe = d + timedelta(days=i)
            if not _in_window(probe, start, end):
                return i
        return 0
    return 0


# ---------------------------------------------------------------- тематичность

def is_thematic(text: str, event: Event | None) -> bool:
    """Есть ли в тексте ключевые слова события."""
    if not event or not event.keywords:
        return False
    low = (text or "").lower()
    return any(k in low for k in event.keywords)


def game_is_thematic(details: dict, event: Event | None) -> bool:
    """Тематическая ли игра: название + жанры + описание."""
    if not event or not event.keywords:
        return False
    haystack = " ".join([
        str(details.get("name") or ""),
        " ".join(details.get("genres") or []),
        str(details.get("short_desc_raw") or details.get("short_desc") or ""),
    ])
    return is_thematic(haystack, event)


def boost_for(details: dict, event: Event | None) -> int:
    """Бонус к весу игры. Только порядок очереди — никакого отсева."""
    if not event:
        return 0
    return event.boost if game_is_thematic(details, event) else 0


def describe(event: Event | None) -> str:
    """Человеческая строка для /status и /check."""
    if not event:
        nxt = next_event()
        if not nxt:
            return "обычные дни"
        ev, when = nxt
        return f"обычные дни · следующее: {ev.label} с {when.strftime('%d.%m')}"
    left = days_left()
    tail = f" · ещё {left} дн." if left else ""
    return f"{event.label}{tail}"
