"""Store behaviour. No API, no network."""

import pytest

from intake import db


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "test.db")
    yield c
    c.close()


def _submission(conn, sid="SUB-0001", message_id="<a@b.com>"):
    conn.execute(
        "INSERT INTO submissions (id, message_id, received_at) VALUES (?,?,?)",
        (sid, message_id, "2026-07-30T09:00:00"),
    )
    return sid


# --- schema -----------------------------------------------------------------


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    db.init(path).close()
    conn = db.init(path)  # second call must not raise
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {"brokers", "submissions", "documents", "extracts",
            "clearance", "decisions", "scores", "events"} <= tables


def test_message_id_is_unique(conn):
    _submission(conn)
    with pytest.raises(Exception):
        _submission(conn, sid="SUB-0002", message_id="<a@b.com>")


def test_foreign_keys_enforced(conn):
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO documents (submission_id, filename) VALUES (?,?)",
            ("SUB-NOPE", "acord.pdf"),
        )


# --- content hash: the thing that makes re-runs free ------------------------


def test_hash_is_order_independent():
    assert db.content_hash(["a", "b"]) == db.content_hash(["b", "a"])


def test_hash_changes_with_content():
    assert db.content_hash(["a", "b"]) != db.content_hash(["a", "b!"])


def test_hash_is_not_fooled_by_concatenation():
    """'ab' + '' must not collide with 'a' + 'b'."""
    assert db.content_hash(["ab", ""]) != db.content_hash(["a", "b"])


# --- status transitions and audit trail -------------------------------------


def test_set_status_rejects_unknown_state(conn):
    _submission(conn)
    with pytest.raises(ValueError, match="Unknown status"):
        db.set_status(conn, "SUB-0001", "sideways")


def test_set_status_records_the_transition(conn):
    _submission(conn)
    db.set_status(conn, "SUB-0001", "parsed")
    db.set_status(conn, "SUB-0001", "extracted")
    rows = conn.execute(
        "SELECT event_type, detail FROM events WHERE submission_id=? ORDER BY id",
        ("SUB-0001",),
    ).fetchall()
    assert [r["event_type"] for r in rows] == ["status_changed", "status_changed"]
    assert '"from": "received"' in rows[0]["detail"]
    assert '"to": "extracted"' in rows[1]["detail"]


def test_events_capture_the_actor(conn):
    _submission(conn)
    db.set_status(conn, "SUB-0001", "declined", actor="jbhadane")
    row = conn.execute(
        "SELECT actor FROM events WHERE submission_id=? ORDER BY id DESC LIMIT 1",
        ("SUB-0001",),
    ).fetchone()
    assert row["actor"] == "jbhadane"


def test_log_event_serialises_structured_detail(conn):
    _submission(conn)
    db.log_event(conn, "SUB-0001", "override", {"rule": "PROHIBITED_CLASS"})
    row = conn.execute("SELECT detail FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["detail"] == '{"rule": "PROHIBITED_CLASS"}'


def test_session_rolls_back_on_error(tmp_path):
    path = tmp_path / "rb.db"
    with pytest.raises(RuntimeError):
        with db.session(path) as c:
            _submission(c)
            raise RuntimeError("boom")
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) n FROM submissions").fetchone()["n"] == 0
    conn.close()
