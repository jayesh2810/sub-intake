"""Stage 2 - parse.

Turns the files in a packet into text the extraction agent can read, and
labels each one so the agent knows what it is looking at before it opens it.

The important behaviour here is what happens to a scan. A PDF with no text
layer yields an empty string, and an empty string looks exactly like a
document that says nothing. Silently storing that would let a submission
reach an underwriter with its loss history quietly missing. So a page that
produces no text is flagged `needs_ocr` and the submission is marked for
manual handling rather than guessed at.

Classification is regex over the extracted text, with the filename as a
fallback for documents we cannot read. Deterministic and free - there is no
reason to spend a model call deciding whether something is a loss run.
"""

import re
import sqlite3
from pathlib import Path
from typing import Optional

import pdfplumber

from intake import db
from intake.models import DocumentType

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Below this a page is treated as having no recoverable text.
MIN_CHARS_PER_PAGE = 25

# Ordered: first match wins, so the specific patterns come before the loose
# ones. A loss run mentioning "ACORD" must not be classified as an ACORD.
_CONTENT_PATTERNS: list[tuple[DocumentType, re.Pattern]] = [
    (DocumentType.LOSS_RUN, re.compile(
        r"loss\s+(run|experience|history)|policy\s+year.*incurred|"
        r"valued\s+as\s+of.*claims?", re.I | re.S)),
    (DocumentType.SOV, re.compile(
        r"statement\s+of\s+values|total\s+insurable\s+value|"
        r"schedule\s+of\s+locations", re.I)),
    (DocumentType.ACORD_140, re.compile(r"acord\s*140|property\s+section", re.I)),
    (DocumentType.ACORD_126, re.compile(r"acord\s*126|general\s+liability\s+section", re.I)),
    (DocumentType.ACORD_125, re.compile(
        r"acord\s*125|commercial\s+insurance\s+application|"
        r"applicant\s+information", re.I)),
    (DocumentType.FINANCIAL, re.compile(
        r"balance\s+sheet|income\s+statement|profit\s+and\s+loss|"
        r"statement\s+of\s+cash\s+flows", re.I)),
    (DocumentType.SUPPLEMENTAL, re.compile(
        r"supplemental\s+application|supplemental\s+questionnaire", re.I)),
]

_FILENAME_PATTERNS: list[tuple[DocumentType, re.Pattern]] = [
    (DocumentType.EMAIL_BODY, re.compile(r"email_body", re.I)),
    (DocumentType.LOSS_RUN, re.compile(r"loss[_-]?run", re.I)),
    (DocumentType.SOV, re.compile(r"\bsov\b|statement[_-]?of[_-]?values", re.I)),
    (DocumentType.ACORD_140, re.compile(r"acord[_-]?140", re.I)),
    (DocumentType.ACORD_126, re.compile(r"acord[_-]?126", re.I)),
    (DocumentType.ACORD_125, re.compile(r"acord[_-]?125", re.I)),
    (DocumentType.FINANCIAL, re.compile(r"financial|balance|income", re.I)),
    (DocumentType.SUPPLEMENTAL, re.compile(r"supplement", re.I)),
]


def classify(filename: str, text: Optional[str]) -> DocumentType:
    """Label a document from its content, falling back to its filename."""
    if filename.lower().endswith(".txt") and "email_body" in filename.lower():
        return DocumentType.EMAIL_BODY
    if text and len(text.strip()) >= MIN_CHARS_PER_PAGE:
        for doc_type, pattern in _CONTENT_PATTERNS:
            if pattern.search(text):
                return doc_type
    for doc_type, pattern in _FILENAME_PATTERNS:
        if pattern.search(filename):
            return doc_type
    return DocumentType.UNKNOWN


def extract_pdf(path: Path) -> tuple[str, int, bool]:
    """Return (text, page_count, needs_ocr).

    needs_ocr is True when the PDF carries no recoverable text layer - a scan.
    """
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    text = "\n".join(pages).strip()
    readable = sum(1 for p in pages if len(p.strip()) >= MIN_CHARS_PER_PAGE)
    return text, page_count, readable == 0


def parse_submission(conn: sqlite3.Connection, submission_id: str) -> dict:
    """Parse every file in one packet. Idempotent - re-parsing replaces rows."""
    row = conn.execute(
        "SELECT packet_dir FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"No submission '{submission_id}'")

    packet = PROJECT_ROOT / row["packet_dir"]
    if not packet.is_dir():
        raise FileNotFoundError(f"Packet directory missing: {packet}")

    conn.execute("DELETE FROM documents WHERE submission_id = ?", (submission_id,))

    parsed, unreadable = 0, []
    for path in sorted(packet.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue

        if path.suffix.lower() == ".pdf":
            try:
                text, page_count, needs_ocr = extract_pdf(path)
                method = "none" if needs_ocr else "pdfplumber"
            except Exception as exc:  # a corrupt attachment must not stop the run
                text, page_count, needs_ocr, method = "", 0, True, f"error: {exc}"
        elif path.suffix.lower() in {".txt", ".md"}:
            text, page_count, needs_ocr, method = path.read_text(), 1, False, "text"
        else:
            text, page_count, needs_ocr, method = "", 0, True, "unsupported"

        doc_type = classify(path.name, text)
        conn.execute(
            """INSERT INTO documents
                 (submission_id, filename, doc_type, page_count, text,
                  extraction_method, needs_ocr)
               VALUES (?,?,?,?,?,?,?)""",
            (submission_id, path.name, doc_type.value, page_count, text,
             method, int(needs_ocr)),
        )
        parsed += 1
        if needs_ocr:
            unreadable.append(path.name)

    if unreadable:
        # Loud, not silent. An unreadable loss run is a missing loss run.
        db.log_event(conn, submission_id, "documents_unreadable",
                     {"files": unreadable, "reason": "no recoverable text layer"})
    db.set_status(conn, submission_id, "parsed")
    return {"submission_id": submission_id, "documents": parsed,
            "needs_ocr": unreadable}


def run(conn: sqlite3.Connection, only: Optional[list[str]] = None) -> dict:
    """Parse every submission that has been ingested but not yet parsed."""
    if only:
        ids = list(only)
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM submissions WHERE status = 'received' ORDER BY id")]

    results = [parse_submission(conn, sid) for sid in ids]
    flagged = [r["submission_id"] for r in results if r["needs_ocr"]]
    return {
        "parsed": len(results),
        "documents": sum(r["documents"] for r in results),
        "needs_ocr": flagged,
    }
