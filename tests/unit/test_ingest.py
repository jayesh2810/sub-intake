"""Ingest stage. No API, no network."""

from email.message import EmailMessage

import pytest

from intake import db
from intake.pipeline import ingest


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "PACKETS_DIR", tmp_path / "packets")
    monkeypatch.setattr(ingest, "PROJECT_ROOT", tmp_path)
    c = db.init(tmp_path / "test.db")
    yield c
    c.close()


def build_eml(message_id="<a@broker.com>", sender="Devanshi Patel <d.patel@hb.com>",
              subject="New Submission", body="Please see attached.",
              attachments=(("acord_125.pdf", b"%PDF-1.4 fake"),)) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = sender
    msg["To"] = "newsubmissions@carrier.com"
    msg["Date"] = "Mon, 27 Jul 2026 09:00:00 -0500"
    msg["Subject"] = subject
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


# --- idempotency ------------------------------------------------------------


def test_same_message_ingests_once(conn):
    raw = build_eml()
    first, new1 = ingest.ingest_message(conn, raw)
    second, new2 = ingest.ingest_message(conn, raw)
    assert new1 is True and new2 is False
    assert first == second
    assert conn.execute("SELECT COUNT(*) n FROM submissions").fetchone()["n"] == 1


def test_different_messages_get_different_ids(conn):
    a, _ = ingest.ingest_message(conn, build_eml(message_id="<one@x.com>"))
    b, _ = ingest.ingest_message(conn, build_eml(message_id="<two@x.com>"))
    assert a != b


def test_display_ids_are_sequential(conn):
    ids = [ingest.ingest_message(conn, build_eml(message_id=f"<{i}@x.com>"))[0]
           for i in range(3)]
    assert ids == ["SUB-0001", "SUB-0002", "SUB-0003"]


def test_missing_message_id_still_ingests(conn):
    """A malformed mail is still a submission. Dropping it silently loses premium."""
    raw = build_eml(message_id="")
    sid, was_new = ingest.ingest_message(conn, raw)
    assert was_new is True
    # and it must be recognised on a second pass rather than duplicating
    _, again = ingest.ingest_message(conn, raw)
    assert again is False


# --- attachment handling ----------------------------------------------------


def test_attachments_and_body_are_written(conn, tmp_path):
    sid, _ = ingest.ingest_message(conn, build_eml())
    packet = tmp_path / "packets" / sid
    assert (packet / "acord_125.pdf").read_bytes() == b"%PDF-1.4 fake"
    body = (packet / "email_body.txt").read_text()
    assert "Please see attached." in body
    assert "Subject: New Submission" in body


@pytest.mark.parametrize(
    "given,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("/absolute/path.pdf", "path.pdf"),
        ("loss runs (2026).pdf", "loss_runs__2026_.pdf"),
        ("", "fallback.bin"),
        (None, "fallback.bin"),
        ("...", "fallback.bin"),
    ],
)
def test_attachment_names_cannot_escape_the_packet(given, expected):
    assert ingest.safe_filename(given, "fallback.bin") == expected


def test_traversal_attachment_stays_inside_packet(conn, tmp_path):
    sid, _ = ingest.ingest_message(
        conn, build_eml(attachments=(("../../../evil.pdf", b"x"),))
    )
    packet = tmp_path / "packets" / sid
    assert (packet / "evil.pdf").is_file()
    assert not (tmp_path / "evil.pdf").exists()


# --- broker matching --------------------------------------------------------


def _broker(conn):
    conn.execute(
        """INSERT INTO brokers (id, name, agency, email, submissions_sent, bound)
           VALUES ('BRK-001','Devanshi Patel','Harborline','d.patel@hb.com',84,31)"""
    )


def test_broker_matched_by_email(conn):
    _broker(conn)
    sid, _ = ingest.ingest_message(conn, build_eml(sender="Whoever <d.patel@hb.com>"))
    row = conn.execute("SELECT broker_id FROM submissions WHERE id=?", (sid,)).fetchone()
    assert row["broker_id"] == "BRK-001"


def test_broker_matched_by_agency_domain(conn):
    """A submissions@ mailbox is still that agency's submission."""
    _broker(conn)
    sid, _ = ingest.ingest_message(conn, build_eml(sender="Submissions <subs@hb.com>"))
    row = conn.execute("SELECT broker_id FROM submissions WHERE id=?", (sid,)).fetchone()
    assert row["broker_id"] == "BRK-001"


def test_unknown_sender_leaves_broker_null(conn):
    _broker(conn)
    sid, _ = ingest.ingest_message(conn, build_eml(sender="Nobody <n@elsewhere.com>"))
    row = conn.execute("SELECT broker_id FROM submissions WHERE id=?", (sid,)).fetchone()
    assert row["broker_id"] is None


# --- audit ------------------------------------------------------------------


def test_ingest_is_recorded(conn):
    sid, _ = ingest.ingest_message(conn, build_eml(), source="SUB-0001.eml")
    row = conn.execute(
        "SELECT event_type, detail FROM events WHERE submission_id=?", (sid,)
    ).fetchone()
    assert row["event_type"] == "ingested"
    assert '"attachments": 1' in row["detail"]


def test_missing_mailbox_is_a_clear_error(conn, tmp_path):
    with pytest.raises(FileNotFoundError, match="generate_mailbox"):
        ingest.run(conn, mailbox=tmp_path / "nope")
