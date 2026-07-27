"""SQLite store. One row per catalyst record, deduped on a content hash.

Dedupe matters more than it sounds: the same trial update will reappear in every
weekly pull for as long as it sits in the lookback window, and CHMP items get
republished. Without hashing you re-screen (and re-pay for) the same records.
"""

import datetime as _dt
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

-- Last-seen phase/status per trial, keyed on NCT ID. Separate from `records`
-- because it is current state, not an event: exactly one row per trial, updated
-- in place. Diffing this against an incoming pull is what turns a stream of
-- "here is the phase again" updates into "this trial moved Ph2 -> Ph3".
CREATE TABLE IF NOT EXISTS trial_state (
    nct        TEXT PRIMARY KEY,
    phase      TEXT,
    status     TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    updated    TEXT DEFAULT CURRENT_TIMESTAMP
);
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


def get_trial_states(ncts=None) -> dict:
    """Last-seen {phase, status} keyed by NCT ID. Empty dict for unseen trials."""
    ncts = [n for n in (ncts or []) if n]
    if not ncts:
        return {}
    out = {}
    with db() as conn:
        # chunked so a large pull cannot exceed SQLite's variable limit
        for i in range(0, len(ncts), 500):
            chunk = ncts[i:i + 500]
            qs = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT nct, phase, status FROM trial_state WHERE nct IN ({qs})",
                chunk,
            ):
                out[r["nct"]] = {"phase": r["phase"], "status": r["status"]}
    return out


def upsert_trial_states(rows) -> None:
    """Record current phase/status per NCT. `rows` is an iterable of
    (nct, phase, status). Called AFTER transitions have been computed."""
    rows = [(n, p, s) for n, p, s in rows if n]
    if not rows:
        return
    with db() as conn:
        conn.executemany(
            "INSERT INTO trial_state (nct, phase, status) VALUES (?, ?, ?) "
            "ON CONFLICT(nct) DO UPDATE SET phase=excluded.phase, "
            "status=excluded.status, updated=CURRENT_TIMESTAMP",
            rows,
        )


def to_feed_lines(records) -> str:
    """Render records in the pipe-delimited format the screening tool expects.

    This format is a contract with the React screening artifact — do not change
    it. The human-readable version lives in to_digest() instead.
    """
    return "\n".join(
        f"{r['date']} | {r['source']} | {r['text']}" for r in records
    )


def to_digest(records) -> str:
    """Human-readable weekly digest: leads first, then everything by source.

    Ranking is NOT done here — that belongs to the screening tool. This only
    surfaces universe matches and phase transitions, which are already-computed
    flags, so nothing about the scoring philosophy moves into this pipeline.
    """
    if not records:
        return "No new records this run.\n"

    def line(r):
        return f"  {r['date']}  {r['source']:<22}  {r['text']}"

    today = _dt.date.today().isoformat()
    out = [f"RADIOPHARMA CATALYST DIGEST — {today}",
           "=" * 78,
           f"{len(records)} record(s) awaiting screening"]

    transitions = [r for r in records if "PHASE TRANSITION" in r["text"]]
    leads = [r for r in records if r["in_universe"] and r not in transitions]

    if transitions:
        out += ["", f"PHASE TRANSITIONS ({len(transitions)}) — trials that moved",
                "-" * 78]
        out += [line(r) for r in sorted(transitions, key=_by_date)]

    if leads:
        out += ["", f"WATCHLIST MATCHES ({len(leads)})", "-" * 78]
        out += [line(r) for r in sorted(leads, key=_by_date)]

    rest = [r for r in records if r not in transitions and r not in leads]
    if rest:
        out += ["", f"OTHER ({len(rest)}) — by source", "-" * 78]
        for src in sorted({r["source"] for r in rest}):
            out.append(f"\n  [{src}]")
            out += [line(r) for r in sorted(
                [r for r in rest if r["source"] == src], key=_by_date)]

    out.append("")
    return "\n".join(out) + "\n"


def _by_date(r):
    return (r["date"], r["source"])
