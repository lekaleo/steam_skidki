"""Конфигурация из .env"""
import os
from dataclasses import dataclass, field
from datetime import timezone, timedelta

from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


# РОВНО 4 поста в день. Никаких extra-рубрик: только multi / single / news.
DEFAULT_SCHEDULE = "12:00 multi, 15:00 single, 18:00 news, 21:00 single"

# разрешённые рубрики расписания (всё остальное игнорируется при разборе SCHEDULE)
SCHEDULE_KINDS = ("multi", "single", "news")


@dataclass
class Config:
    bot_token: str = _get("BOT_TOKEN", "")
    channel: str = _get("CHANNEL", "@steam_skidki")
    channel_title: str = _get("CHANNEL_TITLE", "STEAM | СКИДКИ")
    owner_id: int = int(_get("OWNER_ID", "0") or 0)

    # чат обсуждений (супергруппа комментариев). Можно не задавать — модератор ловит
    # любые group/supergroup апдейты, где сообщение пришло.
    discuss_chat_id: int = int(_get("DISCUSS_CHAT_ID", "0") or 0)

    max_search_pages: int = int(_get("MAX_SEARCH_PAGES", "80") or 0)
    min_discount: int = int(_get("MIN_DISCOUNT", "15") or 0)
    steam_delay: float = float(_get("STEAM_DELAY", "0.4"))

    tz_offset: int = int(_get("TIMEZONE_OFFSET", "3"))
    post_delay: float = float(_get("POST_DELAY", "10"))

    # "12:00 multi, 15:00 single, 18:00 news, 21:00 single"
    # строго 4 поста в день: подборка, две игры, новость.
    schedule: list = field(default_factory=lambda: _parse_schedule(
        _get("SCHEDULE", DEFAULT_SCHEDULE) or DEFAULT_SCHEDULE))

    # синхронизация каждые 4 часа = 6 раз в сутки
    sync_hours: int = int(_get("SYNC_HOURS", "4"))

    # премиум-эмодзи Telegram (можно выключить, если у бота нет прав)
    premium_emoji: bool = _get("PREMIUM_EMOJI", "1") in ("1", "true", "True", "yes")
    # свои проверенные emoji-id: "fire=5368324170671202286,bolt=5456140674028019486"
    custom_emoji_map: dict = field(default_factory=lambda: _parse_emoji_map(_get("CUSTOM_EMOJI_MAP", "")))

    # минимальный вес игры (популярность+отзывы+скидка). 0 = без отсева, только сортировка
    min_score: int = int(_get("MIN_SCORE", "0") or 0)
    # лимит длины подписи к фото в Telegram (символы)
    caption_limit: int = int(_get("CAPTION_LIMIT", "1000") or 1000)

    data_dir: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "data"))
    img_dir: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "images"))
    db_path: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "state.db"))
    assets_dir: str = os.path.abspath(os.path.join(os.path.dirname(__file__), "assets"))

    # размер карточки (высокое разрешение)
    card_w: int = 1280
    card_h: int = 720

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.tz_offset))

    @property
    def channel_handle(self) -> str:
        return self.channel.lstrip("@")

    def ensure_dirs(self):
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.img_dir, exist_ok=True)
        os.makedirs(os.path.join(self.assets_dir, "fonts"), exist_ok=True)

    def mask(self, text) -> str:
        """Маскирует токен бота в любой строке (для безопасных логов)."""
        s = str(text)
        if self.bot_token:
            s = s.replace(self.bot_token, "***TOKEN***")
        return s

    @property
    def footer(self) -> str:
        return f"🔵 {self.channel_title} — подписаться"


def _parse_schedule(raw: str) -> list:
    """Разбирает SCHEDULE и жёстко отбрасывает всё, кроме multi/single/news."""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        if len(bits) != 2:
            continue
        try:
            hh, mm = bits[0].split(":")
            hh, mm = int(hh), int(mm)
        except ValueError:
            continue
        kind = bits[1].strip().lower()
        if kind not in SCHEDULE_KINDS:
            continue
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue
        out.append({"time": (hh, mm), "kind": kind})
    if not out:
        # пустой/битый SCHEDULE не должен оставить канал без постов
        out = [{"time": (12, 0), "kind": "multi"},
               {"time": (15, 0), "kind": "single"},
               {"time": (18, 0), "kind": "news"},
               {"time": (21, 0), "kind": "single"}]
    return sorted(out, key=lambda x: x["time"])


def _parse_emoji_map(raw: str) -> dict:
    """CUSTOM_EMOJI_MAP=fire=5368...,bolt=5456... -> {"fire": "5368...", ...}"""
    out: dict = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if "=" not in part:
            continue
        name, eid = part.split("=", 1)
        name, eid = name.strip(), eid.strip()
        if name and eid.isdigit():
            out[name] = eid
    return out


cfg = Config()
