"""SQLite store. One row per catalyst record, deduped on a content hash.

Dedupe matters more than it sounds: the same trial update will reappear in every
weekly pull for as long as it sits in the lookback window, and CHMP items get
republished. Without hashing you re-screen (and re-pay for) the same records.
"""

import hashlib
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    hash        TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    source      TEXT NOT NULL,
    ref         TEXT,
    text        TEXT NOT NULL,
    in_universe INTEGER DEFAULT 0,
    first_seen  TEXT DEFAULT CURRENT_TIMESTAMP,
    screened    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_date ON records(date);
CREATE INDEX IF NOT EXISTS idx_screened ON records(screened);
"""


def content_hash(source: str, ref: str, text: str) -> str:
    key = f"{source}|{ref}|{text}".lower().strip()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_many(records) -> int:
    """Insert records, skipping ones already seen. Returns count of new rows."""
    new = 0
    with db() as conn:
        for r in records:
            h = content_hash(r["source"], r.get("ref", ""), r["text"])
            cur = conn.execute(
                "INSERT OR IGNORE INTO records (hash, date, source, ref, text, in_universe) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (h, r["date"], r["source"], r.get("ref", ""), r["text"],
                 int(r.get("in_universe", False))),
            )
            new += cur.rowcount
    return new


def unscreened(limit: int = 200):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM records WHERE screened = 0 ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_screened(hashes):
    with db() as conn:
        conn.executemany(
            "UPDATE records SET screened = 1 WHERE hash = ?", [(h,) for h in hashes]
        )


def to_feed_lines(records) -> str:
    """Render records in the pipe-delimited format the screening tool expects."""
    return "\n".join(
        f"{r['date']} | {r['source']} | {r['text']}" for r in records
    )
