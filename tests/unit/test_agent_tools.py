"""Agent tools. No API calls - the tools themselves never touch a model.

What matters here is what the agent is told about a document it cannot read.
An empty string would read as "this document says nothing", and the agent
would faithfully record no losses. It has to be told the difference.
"""

import pytest

from intake import db
from intake.agent import tools


@pytest.fixture
def bound(tmp_path):
    path = tmp_path / "test.db"
    conn = db.init(path)
    conn.execute(
        "INSERT INTO submissions (id, message_id, received_at) "
        "VALUES ('SUB-0001','<a@b>','2026-07-27')"
    )
    conn.commit()
    tools.bind(path)
    yield conn
    conn.close()
    tools.bind(None)


def add(conn, filename, doc_type, text, needs_ocr=0, pages=1):
    conn.execute(
        """INSERT INTO documents (submission_id, filename, doc_type, page_count,
                                  text, extraction_method, needs_ocr)
           VALUES ('SUB-0001',?,?,?,?,?,?)""",
        (filename, doc_type, pages, text, "pdfplumber", needs_ocr),
    )
    conn.commit()


def call(t, **kwargs) -> str:
    return t.run(**kwargs)


# --- list_documents ---------------------------------------------------------


def test_lists_type_and_size(bound):
    add(bound, "acord_125.pdf", "acord_125", "ACORD 125 " * 40)
    out = call(tools.list_documents, submission_id="SUB-0001")
    assert "acord_125.pdf" in out and "[acord_125]" in out and "chars" in out


def test_unreadable_document_is_announced_as_such(bound):
    add(bound, "loss_runs.pdf", "loss_run", "", needs_ocr=1)
    out = call(tools.list_documents, submission_id="SUB-0001")
    assert "UNREADABLE" in out
    assert "do not guess" in out.lower()


def test_unknown_submission_says_so(bound):
    out = call(tools.list_documents, submission_id="SUB-9999")
    assert "No submission" in out


def test_parsed_but_empty_packet_is_distinguished(bound):
    out = call(tools.list_documents, submission_id="SUB-0001")
    assert "no parsed documents" in out


# --- read_document ----------------------------------------------------------


def test_reads_text(bound):
    add(bound, "acord_125.pdf", "acord_125", "NAME: Bluewater Mfg. Co.")
    out = call(tools.read_document, submission_id="SUB-0001", filename="acord_125.pdf")
    assert out == "NAME: Bluewater Mfg. Co."


def test_reading_a_scan_returns_an_explanation_not_emptiness(bound):
    """The defect this guards: empty text read as 'there were no losses'."""
    add(bound, "loss_runs.pdf", "loss_run", "", needs_ocr=1)
    out = call(tools.read_document, submission_id="SUB-0001", filename="loss_runs.pdf")
    assert "no readable text" in out
    assert "missing" in out.lower()
    assert out.strip() != ""


def test_blank_text_without_the_flag_is_still_treated_as_unreadable(bound):
    add(bound, "sov.pdf", "sov", "   \n  ", needs_ocr=0)
    out = call(tools.read_document, submission_id="SUB-0001", filename="sov.pdf")
    assert "no readable text" in out


def test_wrong_filename_lists_what_is_available(bound):
    add(bound, "acord_125.pdf", "acord_125", "ACORD")
    out = call(tools.read_document, submission_id="SUB-0001", filename="nope.pdf")
    assert "No document 'nope.pdf'" in out
    assert "acord_125.pdf" in out


def test_only_two_tools_are_exposed():
    """Appetite, clearance and scoring are deterministic stages, not agent tools."""
    assert {t.name for t in tools.ALL_TOOLS} == {"list_documents", "read_document"}
