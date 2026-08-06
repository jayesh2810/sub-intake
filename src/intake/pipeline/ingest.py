"""Stage 1 - ingest.

Reads .eml files, writes their attachments to disk, and registers one
submission per message.

The natural key is the RFC 5322 Message-ID, which is what makes this stage
idempotent: re-running over the same mailbox recognises every message and
creates nothing. The `SUB-nnnn` id is a display key assigned on first sight,
kept because an underwriter has to be able to say "look at 0042" out loud.

The email body is written into the packet alongside the attachments. Brokers
put real underwriting content in the covering note - values, framing, what
they are still waiting on - and dropping it would lose facts the PDFs do not
carry.
"""

import email
import re
import sqlite3
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import yaml

from intake import db

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAILBOX_DIR = PROJECT_ROOT / "data" / "mailbox"
PACKETS_DIR = PROJECT_ROOT / "data" / "packets"
BROKERS_FILE = PROJECT_ROOT / "data" / "brokers.yaml"

# Anything that could escape the packet directory or collide with our own
# filenames gets replaced. Attachment names arrive from outside.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(name: Optional[str], fallback: str) -> str:
    """Make an attachment filename safe to write inside a packet directory."""
    if not name:
        return fallback
    cleaned = _UNSAFE.sub("_", Path(name).name).lstrip(".")
    return cleaned or fallback


def load_brokers(conn: sqlite3.Connection) -> int:
    """Load the broker book. Upserts, so re-running refreshes the history."""
    if not BROKERS_FILE.is_file():
        return 0
    book = yaml.safe_load(BROKERS_FILE.read_text()).get("brokers", [])
    for b in book:
        sent, bound = b.get("submissions_sent", 0), b.get("bound", 0)
        conn.execute(
            """INSERT INTO brokers
                 (id, name, agency, email, submissions_sent, bound,
                  hit_ratio, avg_bound_premium, tier)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, agency=excluded.agency, email=excluded.email,
                 submissions_sent=excluded.submissions_sent, bound=excluded.bound,
                 hit_ratio=excluded.hit_ratio,
                 avg_bound_premium=excluded.avg_bound_premium, tier=excluded.tier""",
            (b["id"], b["name"], b["agency"], b.get("email"), sent, bound,
             (bound / sent) if sent else None, b.get("avg_bound_premium_usd"),
             b.get("tier")),
        )
    return len(book)


def _match_broker(conn: sqlite3.Connection, from_header: str) -> Optional[str]:
    """Resolve the sender to a broker in the book, by address then by name."""
    addr = ""
    if "<" in from_header and ">" in from_header:
        addr = from_header.split("<", 1)[1].split(">", 1)[0].strip().lower()
    display = from_header.split("<", 1)[0].strip().strip('"')

    if addr:
        row = conn.execute(
            "SELECT id FROM brokers WHERE lower(email) = ?", (addr,)
        ).fetchone()
        if row:
            return row["id"]
        # Fall back to the agency domain - a submissions@ mailbox is still
        # that agency's submission.
        domain = addr.split("@")[-1]
        row = conn.execute(
            "SELECT id FROM brokers WHERE lower(email) LIKE ?", (f"%@{domain}",)
        ).fetchone()
        if row:
            return row["id"]
    if display:
        row = conn.execute(
            "SELECT id FROM brokers WHERE lower(name) = ?", (display.lower(),)
        ).fetchone()
        if row:
            return row["id"]
    return None


def received_at(date_header: Optional[str]) -> tuple[str, Optional[str]]:
    """Return (ISO 8601 UTC timestamp, warning).

    Stored as ISO because the raw header does not sort: as a string,
    "Fri, 31 Jul" comes before "Mon, 27 Jul". Clearance decides which broker
    owns a risk by who submitted first, so a header-order mistake here would
    hand the account to the wrong agency and nothing downstream would notice.

    A header we cannot read falls back to the ingest time, which is the
    earliest moment we can honestly vouch for, and says so in the event log.
    """
    raw = (date_header or "").strip()
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(), None

    return (
        datetime.now(timezone.utc).isoformat(),
        f"unreadable Date header {raw!r}; recorded as received at ingest",
    )


def _next_display_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM submissions WHERE id LIKE 'SUB-%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    n = int(row["id"].split("-")[1]) + 1 if row else 1
    return f"SUB-{n:04d}"


def ingest_message(
    conn: sqlite3.Connection, raw: bytes, source: str = ""
) -> tuple[str, bool]:
    """Register one message. Returns (submission_id, was_new)."""
    msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]

    message_id = (msg["Message-ID"] or "").strip()
    if not message_id:
        # No Message-ID is a malformed mail, but dropping it silently would
        # lose a submission. Synthesise a stable one from the content.
        message_id = f"<generated-{db.content_hash([raw.decode('utf-8', 'replace')])[:16]}>"

    existing = conn.execute(
        "SELECT id FROM submissions WHERE message_id = ?", (message_id,)
    ).fetchone()
    if existing:
        return existing["id"], False

    submission_id = _next_display_id(conn)
    packet = PACKETS_DIR / submission_id
    packet.mkdir(parents=True, exist_ok=True)

    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        (packet / "email_body.txt").write_text(
            f"From: {msg['From']}\nDate: {msg['Date']}\n"
            f"Subject: {msg['Subject']}\n\n{body.get_content()}"
        )

    written = 0
    for i, part in enumerate(msg.iter_attachments()):  # type: ignore[attr-defined]
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        name = safe_filename(part.get_filename(), f"attachment_{i + 1}.bin")
        (packet / name).write_bytes(payload)
        written += 1

    timestamp, date_warning = received_at(msg["Date"])
    conn.execute(
        """INSERT INTO submissions
             (id, message_id, subject, received_at, broker_id, packet_dir, status)
           VALUES (?,?,?,?,?,?,'received')""",
        (submission_id, message_id, msg["Subject"], timestamp,
         _match_broker(conn, msg["From"] or ""), str(packet.relative_to(PROJECT_ROOT))),
    )
    db.log_event(
        conn, submission_id, "ingested",
        {"source": source, "attachments": written, "message_id": message_id},
    )
    if date_warning:
        db.log_event(conn, submission_id, "date_unreadable", date_warning)
    return submission_id, True


def run(conn: sqlite3.Connection, mailbox: Optional[Path] = None) -> dict:
    """Ingest every message in the mailbox. Safe to re-run."""
    mailbox = Path(mailbox) if mailbox else MAILBOX_DIR
    if not mailbox.is_dir():
        raise FileNotFoundError(
            f"No mailbox at {mailbox}. Generate one:\n"
            f"  python scripts/generate_mailbox.py --count 50 --seed 7"
        )

    brokers = load_brokers(conn)
    new, seen = [], []
    # Sorted so display-id assignment is stable for a given mailbox.
    for path in sorted(mailbox.glob("*.eml")):
        sid, was_new = ingest_message(conn, path.read_bytes(), source=path.name)
        (new if was_new else seen).append(sid)

    return {"brokers": brokers, "ingested": len(new), "already_present": len(seen),
            "new_ids": new}
