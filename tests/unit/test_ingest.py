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


# --- received_at ------------------------------------------------------------
#
# Clearance gives a contested risk to whoever submitted first, and the
# worklist ranks on how long a submission has been sitting. Both read this
# column, and neither would notice if it were wrong.


def test_dates_are_stored_so_that_they_sort():
    """The raw header does not: as a string, 'Fri, 31 Jul' precedes 'Mon, 27 Jul'."""
    headers = ["Mon, 27 Jul 2026 09:00:00 -0500",
               "Thu, 30 Jul 2026 09:00:00 -0500",
               "Fri, 31 Jul 2026 09:00:00 -0500"]
    stored = [ingest.received_at(h)[0] for h in headers]
    assert sorted(stored) == stored
    assert sorted(headers) != headers


def test_offsets_are_normalised_before_comparison():
    """09:00 in Los Angeles is later than 09:00 in New York."""
    east, _ = ingest.received_at("Mon, 27 Jul 2026 09:00:00 -0400")
    west, _ = ingest.received_at("Mon, 27 Jul 2026 09:00:00 -0700")
    assert east < west


def test_an_unreadable_date_falls_back_and_says_so(conn):
    timestamp, warning = ingest.received_at("not a date")
    assert timestamp and warning and "unreadable" in warning

    msg = EmailMessage()
    msg["Message-ID"] = "<bad-date@x.com>"
    msg["From"] = "d.patel@hb.com"
    msg["Date"] = "not a date"
    msg.set_content("hello")
    sid, _ = ingest.ingest_message(conn, msg.as_bytes())

    kinds = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM events WHERE submission_id = ?", (sid,))]
    assert "date_unreadable" in kinds


def test_the_stored_timestamp_is_the_parsed_one(conn):
    submission_id, _ = ingest.ingest_message(conn, build_eml())
    stored = conn.execute(
        "SELECT received_at FROM submissions WHERE id = ?",
        (submission_id,)).fetchone()[0]
    assert stored == "2026-07-27T14:00:00+00:00"


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
