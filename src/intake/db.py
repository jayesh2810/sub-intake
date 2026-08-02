"""SQLite store.

One database holds the whole pipeline: submissions and their documents, the
cached extraction results, the deterministic decisions, and an append-only
event log.

Two things this buys that a pile of JSON files does not. Extraction results are
keyed by a hash of the documents that produced them, so re-running a submission
whose packet has not changed costs nothing and calls no model. And every state
transition lands in `events`, so the file can answer "who decided this, when,
and on what basis" without reconstructing it from logs.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "intake.db"

# Ordered lifecycle. A submission only ever moves forward through the first
# six; the terminal states are mutually exclusive.
PIPELINE_STATES = [
    "received",
    "parsed",
    "extracted",
    "cleared",
    "assessed",
    "scored",
    "queued",
]
TERMINAL_STATES = ["assigned", "declined", "pending_info", "closed"]
ALL_STATES = PIPELINE_STATES + TERMINAL_STATES

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS brokers (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    agency              TEXT NOT NULL,
    email               TEXT,
    phone               TEXT,
    submissions_sent    INTEGER NOT NULL DEFAULT 0,
    bound               INTEGER NOT NULL DEFAULT 0,
    hit_ratio           REAL,
    avg_bound_premium   REAL,
    tier                TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    id              TEXT PRIMARY KEY,
    message_id      TEXT UNIQUE NOT NULL,
    subject         TEXT,
    received_at     TEXT NOT NULL,
    broker_id       TEXT REFERENCES brokers(id),
    packet_dir      TEXT,
    status          TEXT NOT NULL DEFAULT 'received',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_submissions_status ON submissions(status);

CREATE TABLE IF NOT EXISTS documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id       TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    filename            TEXT NOT NULL,
    doc_type            TEXT,
    page_count          INTEGER,
    text                TEXT,
    extraction_method   TEXT,
    needs_ocr           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (submission_id, filename)
);

-- Cached model output. content_hash covers every document that fed the call,
-- so a changed packet misses the cache and an unchanged one always hits.
CREATE TABLE IF NOT EXISTS extracts (
    submission_id   TEXT PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
    content_hash    TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    model           TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_extracts_hash ON extracts(content_hash);

CREATE TABLE IF NOT EXISTS clearance (
    submission_id           TEXT PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
    normalised_name         TEXT NOT NULL,
    normalised_address      TEXT,
    matched_submission_id   TEXT REFERENCES submissions(id),
    match_score             REAL,
    is_duplicate            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_clearance_name ON clearance(normalised_name);

CREATE TABLE IF NOT EXISTS decisions (
    submission_id   TEXT PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
    outcome         TEXT NOT NULL,
    fired_rules_json TEXT NOT NULL,
    queue           TEXT,
    priority        INTEGER,
    guide_version   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scores (
    submission_id   TEXT PRIMARY KEY REFERENCES submissions(id) ON DELETE CASCADE,
    total           REAL NOT NULL,
    components_json TEXT NOT NULL,
    weights_version TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_scores_total ON scores(total DESC);

-- Append only. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   TEXT REFERENCES submissions(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    detail          TEXT,
    actor           TEXT NOT NULL DEFAULT 'system',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_events_submission ON events(submission_id, id);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: Optional[Path] = None) -> sqlite3.Connection:
    """Create the schema if it is not already there. Safe to call repeatedly."""
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    conn = init(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def content_hash(parts: list[str]) -> str:
    """Stable hash over document contents, order-independent.

    Sorted so that re-parsing a packet in a different filesystem order still
    hits the cache.
    """
    h = hashlib.sha256()
    for part in sorted(parts):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def log_event(
    conn: sqlite3.Connection,
    submission_id: Optional[str],
    event_type: str,
    detail: Any = None,
    actor: str = "system",
) -> None:
    """Append to the audit trail. Never overwrites."""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    conn.execute(
        "INSERT INTO events (submission_id, event_type, detail, actor) VALUES (?,?,?,?)",
        (submission_id, event_type, detail, actor),
    )


def set_status(
    conn: sqlite3.Connection, submission_id: str, status: str, actor: str = "system"
) -> None:
    """Move a submission to a new state and record the transition."""
    if status not in ALL_STATES:
        raise ValueError(f"Unknown status '{status}'. Valid: {', '.join(ALL_STATES)}")
    row = conn.execute(
        "SELECT status FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    previous = row["status"] if row else None
    conn.execute(
        "UPDATE submissions SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, submission_id),
    )
    log_event(
        conn, submission_id, "status_changed", {"from": previous, "to": status}, actor
    )
