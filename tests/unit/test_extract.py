"""Extraction cache and stage control. No API calls.

The cache is the load-bearing part of this stage. If it is wrong in the
direction of over-caching, a changed packet silently keeps a stale extract.
If it is wrong the other way, every downstream iteration re-bills the whole
mailbox against a 20-request daily cap.
"""

import json

import pytest

from intake import db
from intake.models import SubmissionExtract
from intake.pipeline import extract


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "FIXTURES_DIR", tmp_path / "fixtures")
    c = db.init(tmp_path / "test.db")
    c.execute(
        "INSERT INTO submissions (id, message_id, received_at, status) "
        "VALUES ('SUB-0001','<a@b>','2026-07-27','parsed')"
    )
    yield c
    c.close()


def add_doc(conn, filename="acord_125.pdf", text="ACORD 125", needs_ocr=0,
            sid="SUB-0001"):
    conn.execute(
        """INSERT INTO documents (submission_id, filename, doc_type, page_count,
                                  text, extraction_method, needs_ocr)
           VALUES (?,?,'acord_125',1,?, 'pdfplumber', ?)""",
        (sid, filename, text, needs_ocr),
    )


def sample_extract(**over) -> SubmissionExtract:
    data = {"broker": {}, "insured": {"named_insured": "Bluewater Mfg. Co."},
            "extraction_confidence": 0.9}
    data.update(over)
    return SubmissionExtract.model_validate(data)


# --- hashing ----------------------------------------------------------------


def test_hash_is_stable_for_an_unchanged_packet(conn):
    add_doc(conn)
    assert extract.packet_hash(conn, "SUB-0001") == extract.packet_hash(conn, "SUB-0001")


def test_hash_changes_when_document_text_changes(conn):
    add_doc(conn)
    before = extract.packet_hash(conn, "SUB-0001")
    conn.execute("UPDATE documents SET text='ACORD 125 revised'")
    assert extract.packet_hash(conn, "SUB-0001") != before


def test_hash_changes_when_a_document_is_added(conn):
    add_doc(conn)
    before = extract.packet_hash(conn, "SUB-0001")
    add_doc(conn, filename="loss_runs.pdf", text="LOSS RUN")
    assert extract.packet_hash(conn, "SUB-0001") != before


def test_hash_changes_when_a_scan_becomes_readable(conn):
    """OCR arriving later must invalidate the extract, not be ignored."""
    add_doc(conn, filename="loss_runs.pdf", text="", needs_ocr=1)
    before = extract.packet_hash(conn, "SUB-0001")
    conn.execute("UPDATE documents SET text='LOSS RUN 2024 $50,000', needs_ocr=0")
    assert extract.packet_hash(conn, "SUB-0001") != before


def test_hash_changes_when_a_document_is_renamed(conn):
    add_doc(conn)
    before = extract.packet_hash(conn, "SUB-0001")
    conn.execute("UPDATE documents SET filename='acord_126.pdf'")
    assert extract.packet_hash(conn, "SUB-0001") != before


# --- cache ------------------------------------------------------------------


def test_cache_miss_when_nothing_stored(conn):
    add_doc(conn)
    assert extract.cached(conn, "SUB-0001") is None


def test_cache_hit_returns_the_stored_extract(conn):
    add_doc(conn)
    extract.store(conn, "SUB-0001", sample_extract())
    hit = extract.cached(conn, "SUB-0001")
    assert hit is not None
    assert hit.insured.named_insured == "Bluewater Mfg. Co."


def test_cache_misses_after_the_packet_changes(conn):
    """The failure that would matter: a stale extract served for new documents."""
    add_doc(conn)
    extract.store(conn, "SUB-0001", sample_extract())
    assert extract.cached(conn, "SUB-0001") is not None
    add_doc(conn, filename="loss_runs.pdf", text="LOSS RUN")
    assert extract.cached(conn, "SUB-0001") is None


def test_store_advances_status_and_records_provenance(conn):
    add_doc(conn)
    extract.store(conn, "SUB-0001", sample_extract(), model="gemini/x",
                  tokens_in=100, tokens_out=20)
    row = conn.execute("SELECT model, tokens_in, tokens_out FROM extracts").fetchone()
    assert row["model"] == "gemini/x" and row["tokens_in"] == 100
    status = conn.execute("SELECT status FROM submissions").fetchone()["status"]
    assert status == "extracted"


def test_store_is_idempotent(conn):
    add_doc(conn)
    extract.store(conn, "SUB-0001", sample_extract())
    extract.store(conn, "SUB-0001", sample_extract())
    assert conn.execute("SELECT COUNT(*) n FROM extracts").fetchone()["n"] == 1


# --- stage control ----------------------------------------------------------


def test_dry_run_calls_nothing_and_reports_the_bill(conn):
    add_doc(conn)
    result = extract.run(conn, dry_run=True)
    assert result["would_call"] == 1
    assert result["would_call_ids"] == ["SUB-0001"]
    assert result["extracted"] == 0
    assert conn.execute("SELECT COUNT(*) n FROM extracts").fetchone()["n"] == 0


def test_dry_run_counts_cached_separately(conn):
    add_doc(conn)
    extract.store(conn, "SUB-0001", sample_extract())
    result = extract.run(conn, dry_run=True)
    assert result["cached"] == 1 and result["would_call"] == 0


def test_limit_caps_the_number_of_calls(conn):
    add_doc(conn)
    for i in (2, 3):
        conn.execute(
            "INSERT INTO submissions (id, message_id, received_at, status) "
            f"VALUES ('SUB-000{i}','<{i}@b>','2026-07-27','parsed')")
        add_doc(conn, sid=f"SUB-000{i}")
    result = extract.run(conn, dry_run=True, limit=2)
    assert result["would_call"] == 2


# --- fixture replay ---------------------------------------------------------


def _write_fixture(conn, sid, synthetic):
    extract.FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    (extract.FIXTURES_DIR / f"{sid}.json").write_text(json.dumps({
        "submission_id": sid,
        "content_hash": extract.packet_hash(conn, sid),
        "synthetic": synthetic,
        "extract": sample_extract().model_dump(mode="json"),
    }))


def test_fixture_replay_needs_no_api(conn):
    add_doc(conn)
    _write_fixture(conn, "SUB-0001", synthetic=False)
    result = extract.run(conn, use_fixtures=True)
    assert result["extracted"] == 1
    assert extract.cached(conn, "SUB-0001") is not None


@pytest.mark.parametrize("synthetic,expected",
                         [(True, "fixture:synthetic"), (False, "fixture:model")])
def test_replay_keeps_provenance(conn, synthetic, expected):
    """A synthesised extract must never look like a real model response."""
    add_doc(conn)
    _write_fixture(conn, "SUB-0001", synthetic=synthetic)
    extract.run(conn, use_fixtures=True)
    assert conn.execute("SELECT model FROM extracts").fetchone()["model"] == expected


def test_missing_fixture_is_reported_not_silently_skipped(conn):
    add_doc(conn)
    result = extract.run(conn, use_fixtures=True)
    assert result["extracted"] == 0
    assert result["failed"][0]["error"] == "no fixture recorded"
