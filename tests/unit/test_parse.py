"""Parse stage. No API, no network."""

import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from intake import db
from intake.models import DocumentType
from intake.pipeline import parse


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(parse, "PROJECT_ROOT", tmp_path)
    c = db.init(tmp_path / "test.db")
    yield c
    c.close()


def make_pdf(path, lines, image_only=False):
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    if image_only:
        from PIL import Image, ImageDraw
        from reportlab.lib.utils import ImageReader
        img = Image.new("L", (850, 1100), 245)
        d = ImageDraw.Draw(img)
        for i, line in enumerate(lines):
            d.text((40, 40 + i * 16), line, fill=30)
        c.drawImage(ImageReader(img), 0, 0, width=LETTER[0], height=LETTER[1])
    else:
        y = 720
        for line in lines:
            c.drawString(60, y, line)
            y -= 14
    c.save()
    return path


def register(conn, tmp_path, sid="SUB-0001"):
    packet = tmp_path / "packets" / sid
    packet.mkdir(parents=True, exist_ok=True)
    conn.execute(
        "INSERT INTO submissions (id, message_id, received_at, packet_dir) VALUES (?,?,?,?)",
        (sid, f"<{sid}@x.com>", "2026-07-27", str(packet.relative_to(tmp_path))),
    )
    return packet


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ACORD 125 COMMERCIAL INSURANCE APPLICATION", DocumentType.ACORD_125),
        ("APPLICANT INFORMATION\nNAME (First Named Insured):", DocumentType.ACORD_125),
        ("LOSS EXPERIENCE REPORT\nPOLICY YEAR CLAIMS INCURRED", DocumentType.LOSS_RUN),
        ("STATEMENT OF VALUES\nTOTAL INSURABLE VALUE $14,000,000", DocumentType.SOV),
        ("ACORD 140 PROPERTY SECTION", DocumentType.ACORD_140),
        ("ACORD 126 GENERAL LIABILITY SECTION", DocumentType.ACORD_126),
        ("BALANCE SHEET as of 12/31/2025", DocumentType.FINANCIAL),
        ("SUPPLEMENTAL APPLICATION - CONTRACTORS", DocumentType.SUPPLEMENTAL),
    ],
)
def test_classified_from_content(text, expected):
    assert parse.classify("whatever.pdf", text) is expected


def test_loss_run_mentioning_acord_is_still_a_loss_run():
    """Order matters: the specific pattern has to win."""
    text = "LOSS EXPERIENCE REPORT\nattached to ACORD 125 application\nPOLICY YEAR INCURRED"
    assert parse.classify("doc.pdf", text) is DocumentType.LOSS_RUN


def test_falls_back_to_filename_when_unreadable():
    """A scan has no text, but the filename still tells us what it is."""
    assert parse.classify("loss_runs.pdf", "") is DocumentType.LOSS_RUN
    assert parse.classify("statement_of_values.pdf", None) is DocumentType.SOV


def test_email_body_is_recognised():
    assert parse.classify("email_body.txt", "From: x") is DocumentType.EMAIL_BODY


def test_unrecognisable_is_unknown_not_a_guess():
    assert parse.classify("scan001.pdf", "lorem ipsum dolor sit amet") is DocumentType.UNKNOWN


# --- text extraction --------------------------------------------------------


def test_readable_pdf_yields_text(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", ["STATEMENT OF VALUES", "TOTAL INSURABLE VALUE $1"])
    text, pages, needs_ocr = parse.extract_pdf(p)
    assert "STATEMENT OF VALUES" in text
    assert pages == 1 and needs_ocr is False


def test_image_only_pdf_is_flagged_not_silently_empty(tmp_path):
    """The defect that matters: an unreadable loss run is a missing loss run."""
    p = make_pdf(tmp_path / "scan.pdf", ["LOSS EXPERIENCE REPORT"], image_only=True)
    text, pages, needs_ocr = parse.extract_pdf(p)
    assert needs_ocr is True
    assert text.strip() == ""
    assert pages == 1


# --- parse_submission -------------------------------------------------------


def test_parses_packet_and_advances_status(conn, tmp_path):
    packet = register(conn, tmp_path)
    make_pdf(packet / "acord_125.pdf", ["ACORD 125 COMMERCIAL INSURANCE APPLICATION"])
    (packet / "email_body.txt").write_text("From: broker\n\nPlease quote.")

    result = parse.parse_submission(conn, "SUB-0001")
    assert result["documents"] == 2
    assert result["needs_ocr"] == []
    status = conn.execute("SELECT status FROM submissions WHERE id='SUB-0001'").fetchone()
    assert status["status"] == "parsed"


def test_unreadable_document_is_logged_loudly(conn, tmp_path):
    packet = register(conn, tmp_path)
    make_pdf(packet / "loss_runs.pdf", ["LOSS RUN"], image_only=True)
    result = parse.parse_submission(conn, "SUB-0001")

    assert result["needs_ocr"] == ["loss_runs.pdf"]
    row = conn.execute(
        "SELECT detail FROM events WHERE event_type='documents_unreadable'"
    ).fetchone()
    assert row is not None and "loss_runs.pdf" in row["detail"]


def test_reparsing_replaces_rather_than_duplicates(conn, tmp_path):
    packet = register(conn, tmp_path)
    make_pdf(packet / "acord_125.pdf", ["ACORD 125"])
    parse.parse_submission(conn, "SUB-0001")
    parse.parse_submission(conn, "SUB-0001")
    n = conn.execute(
        "SELECT COUNT(*) n FROM documents WHERE submission_id='SUB-0001'"
    ).fetchone()["n"]
    assert n == 1


def test_corrupt_pdf_does_not_stop_the_run(conn, tmp_path):
    """One bad attachment must not take down a 100 submission Monday."""
    packet = register(conn, tmp_path)
    (packet / "broken.pdf").write_bytes(b"not a pdf at all")
    make_pdf(packet / "acord_125.pdf", ["ACORD 125"])

    result = parse.parse_submission(conn, "SUB-0001")
    assert result["documents"] == 2
    row = conn.execute(
        "SELECT extraction_method, needs_ocr FROM documents WHERE filename='broken.pdf'"
    ).fetchone()
    assert row["needs_ocr"] == 1
    assert row["extraction_method"].startswith("error:")


def test_unsupported_attachment_is_flagged(conn, tmp_path):
    packet = register(conn, tmp_path)
    (packet / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    result = parse.parse_submission(conn, "SUB-0001")
    row = conn.execute(
        "SELECT extraction_method, needs_ocr FROM documents WHERE filename='photo.jpg'"
    ).fetchone()
    assert row["extraction_method"] == "unsupported"
    assert row["needs_ocr"] == 1


def test_unknown_submission_raises(conn):
    with pytest.raises(KeyError):
        parse.parse_submission(conn, "SUB-9999")
