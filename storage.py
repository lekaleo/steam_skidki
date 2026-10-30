"""Хранилище состояния: каталог скидок, опубликованное, счётчики."""
import json
import os
import sqlite3
import time
from dataclasses import dataclass


@dataclass
class Game:
    appid: int
    title: str
    discount: int
    final: float | None
    initial: float | None
    currency: str
    img_url: str = ""
    exp_date: int | None = None  # unix ts окончания скидки
    short_desc: str = ""
    franchise: str = ""
    # качество/популярность: отзывы Steam + вес для сортировки кандидатов
    review_pct: int = 0
    review_count: int = 0
    review_label: str = ""
    score: int = 0

    def key(self):
        return self.appid


class Storage:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        c = self.conn
        # WAL: защита от блокировок при одновременной работе постера и модератора
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen(
                appid INTEGER PRIMARY KEY,
                title TEXT,
                discount INTEGER,
                final REAL,
                initial REAL,
                currency TEXT,
                img_url TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                score INTEGER DEFAULT 0,
                review_pct INTEGER DEFAULT 0,
                review_count INTEGER DEFAULT 0,
                review_label TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS posted(
                appid INTEGER PRIMARY KEY,
                posted_at INTEGER,
                ptype TEXT
            );
            -- новости: ключ = URL статьи, чтобы ни одна новость не повторилась
            CREATE TABLE IF NOT EXISTS posted_news(
                news_key TEXT PRIMARY KEY,
                url TEXT,
                title TEXT,
                posted_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS kv(
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS strikes(
                user_id INTEGER,
                chat_id INTEGER,
                count INTEGER DEFAULT 0,
                last_at INTEGER,
                PRIMARY KEY (user_id, chat_id)
            );
            """
        )
        # миграция старых баз: добавляем недостающие колонки
        have = {r["name"] for r in c.execute("PRAGMA table_info(seen)").fetchall()}
        for col, ddl in (
            ("score", "INTEGER DEFAULT 0"),
            ("review_pct", "INTEGER DEFAULT 0"),
            ("review_count", "INTEGER DEFAULT 0"),
            ("review_label", "TEXT DEFAULT ''"),
        ):
            if col not in have:
                c.execute(f"ALTER TABLE seen ADD COLUMN {col} {ddl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_seen_score ON seen(score DESC)")
        c.commit()

    # ---------- strikes ----------
    def add_strike(self, user_id: int, chat_id: int) -> int:
        """Увеличивает счётчик нарушений, возвращает новый total."""
        now = int(time.time())
        self.conn.execute(
            """INSERT INTO strikes(user_id,chat_id,count,last_at) VALUES(?,?,1,?)
               ON CONFLICT(user_id,chat_id) DO UPDATE SET count=count+1, last_at=excluded.last_at""",
            (user_id, chat_id, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT count FROM strikes WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        ).fetchone()
        return int(row["count"]) if row else 1

    def get_strikes(self, user_id: int, chat_id: int) -> int:
        row = self.conn.execute(
            "SELECT count FROM strikes WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        ).fetchone()
        return int(row["count"]) if row else 0

    # ---------- seen ----------
    def upsert_seen(self, g: Game):
        now = int(time.time())
        self.conn.execute(
            """INSERT INTO seen(appid,title,discount,final,initial,currency,img_url,
                                first_seen,last_seen,score,review_pct,review_count,review_label)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(appid) DO UPDATE SET
                 title=excluded.title,
                 discount=excluded.discount,
                 final=excluded.final,
                 initial=excluded.initial,
                 currency=excluded.currency,
                 img_url=excluded.img_url,
                 last_seen=excluded.last_seen,
                 score=excluded.score,
                 review_pct=excluded.review_pct,
                 review_count=excluded.review_count,
                 review_label=excluded.review_label""",
            (g.appid, g.title, g.discount, g.final, g.initial, g.currency, g.img_url,
             now, now, g.score, g.review_pct, g.review_count, g.review_label),
        )
        self.conn.commit()

    def set_score(self, appid: int, score: int):
        self.conn.execute("UPDATE seen SET score=? WHERE appid=?", (int(score), appid))
        self.conn.commit()

    def seen_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def new_since(self, hours: float) -> list[Game]:
        cutoff = int(time.time()) - int(hours * 3600)
        rows = self.conn.execute(
            "SELECT * FROM seen WHERE first_seen>=? ORDER BY score DESC, discount DESC", (cutoff,)
        ).fetchall()
        return [self._row_to_game(r) for r in rows]

    def candidates(self, min_discount: int, min_score: int = 0,
                   max_age_hours: int = 48) -> list[Game]:
        """Неопубликованные кандидаты: сначала качественные и популярные, потом всё остальное.

        max_age_hours: берём только игры, виденные в Steam за последние N часов.
        Скидки кончаются и цены меняются — игра, пропавшая из выдачи Steam,
        не должна поститься со старой ценой.
        """
        cutoff = int(time.time()) - int(max_age_hours) * 3600
        rows = self.conn.execute(
            """SELECT s.* FROM seen s
               LEFT JOIN posted p ON p.appid=s.appid
               WHERE s.discount>=? AND p.appid IS NULL AND COALESCE(s.score,0)>=?
                 AND s.last_seen>=?
               ORDER BY COALESCE(s.score,0) DESC, s.discount DESC, s.last_seen DESC
               LIMIT 50""",
            (min_discount, int(min_score or 0), cutoff),
        ).fetchall()
        return [self._row_to_game(r) for r in rows]

    def top_candidates(self, n: int, min_discount: int, min_score: int = 0) -> list[Game]:
        return self.candidates(min_discount, min_score)[:n]

    @staticmethod
    def _row_to_game(r) -> Game:
        keys = r.keys()
        return Game(
            appid=r["appid"],
            title=r["title"],
            discount=r["discount"],
            final=r["final"],
            initial=r["initial"],
            currency=r["currency"],
            img_url=r["img_url"],
            score=r["score"] if "score" in keys and r["score"] is not None else 0,
            review_pct=r["review_pct"] if "review_pct" in keys and r["review_pct"] else 0,
            review_count=r["review_count"] if "review_count" in keys and r["review_count"] else 0,
            review_label=(r["review_label"] if "review_label" in keys else "") or "",
        )

    # ---------- posted ----------
    def mark_posted(self, appids: list[int], ptype: str):
        now = int(time.time())
        for a in appids:
            # уже помеченную при выборе игру переводим в реальный тип поста,
            # чтобы по базе было видно, что именно ушло в канал
            self.conn.execute(
                "INSERT INTO posted(appid,posted_at,ptype) VALUES(?,?,?) "
                "ON CONFLICT(appid) DO UPDATE SET ptype=excluded.ptype",
                (a, now, ptype),
            )
        self.conn.commit()

    def posted_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM posted").fetchone()[0]

    def is_posted(self, appid: int) -> bool:
        return self.conn.execute("SELECT 1 FROM posted WHERE appid=?", (appid,)).fetchone() is not None

    def mark_skipped(self, appid: int, reason: str = "skipped"):
        """Помечает игру как обработанную (не публикуя), чтобы бот не брал её снова."""
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO posted(appid,posted_at,ptype) VALUES(?,?,?)",
                (int(appid), int(time.time()), reason),
            )
            self.conn.commit()
        except sqlite3.Error:
            pass

    # ---------- новости ----------
    def is_news_posted(self, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM posted_news WHERE news_key=?", (key,)).fetchone() is not None

    def mark_news_posted(self, key: str, url: str = "", title: str = ""):
        self.conn.execute(
            "INSERT OR IGNORE INTO posted_news(news_key,url,title,posted_at) VALUES(?,?,?,?)",
            (key, url, title, int(time.time())),
        )
        self.conn.commit()

    def posted_news_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM posted_news").fetchone()[0]

    # ---------- kv ----------
    def kv_get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def kv_set_json(self, key: str, obj):
        self.kv_set(key, json.dumps(obj, ensure_ascii=False))

    def kv_del(self, key: str):
        """Удаляет ключ (нужно для /resetemojis)."""
        self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        self.conn.commit()

    def kv_get_json(self, key: str, default=None):
        raw = self.kv_get(key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
