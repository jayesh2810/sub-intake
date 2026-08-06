"""Tools available to the extraction agent.

The agent reads documents that stage 2 already parsed, so these query the
store rather than the filesystem. Nothing here opens a PDF - by the time the
agent runs, every document is text or is flagged unreadable.

Deliberately only two tools. Appetite, class resolution, clearance and scoring
are all deterministic stages that run after extraction, so the agent has no
business calling them. Its judgement is confined to which documents to open
and what they say.
"""

import sqlite3
from typing import Optional

from crewai.tools import tool

from intake import db

# Set by the extract stage before each kickoff. Tools open their own read
# connection so they never hold the writer's transaction open.
_DB_PATH: Optional[str] = None


def bind(path) -> None:
    global _DB_PATH
    _DB_PATH = str(path) if path else None


def _conn() -> sqlite3.Connection:
    return db.connect(_DB_PATH) if _DB_PATH else db.connect()


@tool("list_documents")
def list_documents(submission_id: str) -> str:
    """List the documents in a submission packet.

    Shows each document's type, length and whether it could be read. Call this
    first: it tells you what is worth opening and what is not there at all.
    """
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT filename, doc_type, page_count, needs_ocr, length(text) chars
               FROM documents WHERE submission_id = ? ORDER BY filename""",
            (submission_id,),
        ).fetchall()
        if not rows:
            known = conn.execute(
                "SELECT COUNT(*) n FROM submissions WHERE id = ?", (submission_id,)
            ).fetchone()["n"]
            if not known:
                return f"No submission '{submission_id}'."
            return f"Submission '{submission_id}' has no parsed documents."

        lines = [f"{len(rows)} document(s) in {submission_id}:"]
        for r in rows:
            if r["needs_ocr"]:
                lines.append(
                    f"  {r['filename']}  [{r['doc_type']}]  UNREADABLE - this "
                    f"document could not be converted to text. Treat its contents "
                    f"as absent; do not guess at what it contained."
                )
            else:
                lines.append(
                    f"  {r['filename']}  [{r['doc_type']}]  "
                    f"{r['page_count']} page(s), {r['chars']:,} chars"
                )
        return "\n".join(lines)
    finally:
        conn.close()


@tool("read_document")
def read_document(submission_id: str, filename: str) -> str:
    """Read one document from a submission packet.

    Pass a filename exactly as listed by list_documents.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT text, needs_ocr, doc_type FROM documents "
            "WHERE submission_id = ? AND filename = ?",
            (submission_id, filename),
        ).fetchone()
        if row is None:
            available = [
                r["filename"]
                for r in conn.execute(
                    "SELECT filename FROM documents WHERE submission_id = ? "
                    "ORDER BY filename", (submission_id,))
            ]
            return (f"No document '{filename}' in {submission_id}. "
                    f"Available: {', '.join(available) or 'none'}")
        if row["needs_ocr"] or not (row["text"] or "").strip():
            return (
                f"'{filename}' has no readable text. It is a scan or a corrupt "
                f"file. Record the facts it would have carried as missing - do "
                f"not infer them from elsewhere."
            )
        return row["text"]
    finally:
        conn.close()


ALL_TOOLS = [list_documents, read_document]
