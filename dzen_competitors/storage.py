import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    niche       TEXT NOT NULL,
    description TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    queries     INTEGER DEFAULT 0,
    channels    INTEGER DEFAULT 0,
    articles    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT UNIQUE NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    description     TEXT,
    subscribers     INTEGER,
    relevance       INTEGER,            -- 0..10 от AI
    category        TEXT,               -- профильный/смежный/нерелевантный
    relevance_reason TEXT,              -- объяснение AI
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    channel_slug    TEXT,
    title           TEXT,
    lead            TEXT,
    views           INTEGER,
    views_till_end  INTEGER,            -- дочитывания
    time_to_read_sec INTEGER,
    publication_ts  INTEGER,            -- UNIX timestamp
    run_id          INTEGER,
    collected_at    TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (channel_slug) REFERENCES channels(slug)
);

CREATE INDEX IF NOT EXISTS idx_articles_channel ON articles(channel_slug);
CREATE INDEX IF NOT EXISTS idx_articles_run ON articles(run_id);

-- Откуда был найден канал в этом прогоне (источник + запрос).
CREATE TABLE IF NOT EXISTS channel_hits (
    run_id      INTEGER NOT NULL,
    slug        TEXT NOT NULL,
    source      TEXT NOT NULL,    -- search_publisher | search_article | similar
    query       TEXT,             -- может быть NULL для similar
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, slug, source, query),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Один раз включаем WAL — даёт concurrent reads/writes без блокировок.
        with sqlite3.connect(db_path) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        # timeout=10s — если другой воркер пишет, ждём вместо OperationalError
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- runs ----------

    def start_run(self, niche: str, description: str = "") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs(niche, description, started_at) VALUES (?, ?, ?)",
                (niche, description, _utcnow()),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, *, queries: int, channels: int, articles: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, queries=?, channels=?, articles=? WHERE id=?",
                (_utcnow(), queries, channels, articles, run_id),
            )

    # ---------- channels ----------

    def upsert_channel(
        self,
        slug: str,
        url: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        subscribers: Optional[int] = None,
    ) -> None:
        now = _utcnow()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO channels(slug, url, title, description, subscribers,
                                     first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    url=excluded.url,
                    title=COALESCE(excluded.title, channels.title),
                    description=COALESCE(excluded.description, channels.description),
                    subscribers=COALESCE(excluded.subscribers, channels.subscribers),
                    last_seen_at=excluded.last_seen_at
                """,
                (slug, url, title, description, subscribers, now, now),
            )

    def update_channel_classification(
        self, slug: str, *,
        relevance: Optional[int],
        category: Optional[str],
        reason: Optional[str],
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE channels SET relevance=?, category=?, relevance_reason=? WHERE slug=?",
                (relevance, category, reason, slug),
            )

    def channels_by_slugs(self, slugs: Iterable[str]) -> list[sqlite3.Row]:
        slugs = list(slugs)
        if not slugs:
            return []
        ph = ",".join("?" * len(slugs))
        with self._conn() as c:
            return c.execute(f"SELECT * FROM channels WHERE slug IN ({ph})", slugs).fetchall()

    # ---------- channel_hits ----------

    def record_hit(self, run_id: int, slug: str, source: str, query: Optional[str]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO channel_hits(run_id, slug, source, query, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, slug, source, query or "", _utcnow()),
            )

    def channel_slugs_for_run(self, run_id: int) -> list[str]:
        with self._conn() as c:
            return [r["slug"] for r in c.execute(
                "SELECT DISTINCT slug FROM channel_hits WHERE run_id=?",
                (run_id,),
            ).fetchall()]

    def hit_counts_for_run(self, run_id: int) -> dict[str, int]:
        """Количество разных (source,query)-попаданий по каждому slug."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT slug, COUNT(*) AS n FROM channel_hits WHERE run_id=? GROUP BY slug",
                (run_id,),
            ).fetchall()
        return {r["slug"]: r["n"] for r in rows}

    # ---------- articles ----------

    def upsert_article(
        self, *,
        url: str,
        channel_slug: Optional[str],
        title: Optional[str],
        lead: Optional[str],
        views: Optional[int],
        views_till_end: Optional[int],
        time_to_read_sec: Optional[int],
        publication_ts: Optional[int],
        run_id: int,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO articles(url, channel_slug, title, lead, views, views_till_end,
                                     time_to_read_sec, publication_ts, run_id, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    channel_slug=COALESCE(excluded.channel_slug, articles.channel_slug),
                    title=COALESCE(excluded.title, articles.title),
                    lead=COALESCE(excluded.lead, articles.lead),
                    views=COALESCE(excluded.views, articles.views),
                    views_till_end=COALESCE(excluded.views_till_end, articles.views_till_end),
                    time_to_read_sec=COALESCE(excluded.time_to_read_sec, articles.time_to_read_sec),
                    publication_ts=COALESCE(excluded.publication_ts, articles.publication_ts),
                    collected_at=excluded.collected_at
                """,
                (url, channel_slug, title, lead, views, views_till_end,
                 time_to_read_sec, publication_ts, run_id, _utcnow()),
            )

    def articles_for_channels(self, slugs: Iterable[str], run_id: int) -> list[sqlite3.Row]:
        slugs = list(slugs)
        if not slugs:
            return []
        ph = ",".join("?" * len(slugs))
        with self._conn() as c:
            return c.execute(
                f"SELECT * FROM articles WHERE run_id=? AND channel_slug IN ({ph})",
                [run_id, *slugs],
            ).fetchall()
