"""Stage 3 - extract. The only stage that calls a model.

Everything downstream is a pure function over what this produces, so the
cache here is what makes the rest of the pipeline free to iterate on. An
extract is keyed by a hash of the documents that produced it: change the
packet and it re-runs, leave it alone and it never calls the model again.

That is not an optimisation. Free tier Gemini allows 20 requests per day on
2.5-flash, which is about four submissions. Without the cache, tuning a
scoring weight would mean re-billing the entire mailbox.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from intake import db
from intake.models import SubmissionExtract

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "extracts"


def packet_hash(conn: sqlite3.Connection, submission_id: str) -> str:
    """Hash every document that the agent could read.

    Filename is included so that renaming a document is a change, and
    needs_ocr is included so that a packet whose scan later becomes readable
    correctly misses the cache.
    """
    rows = conn.execute(
        """SELECT filename, needs_ocr, COALESCE(text,'') text
           FROM documents WHERE submission_id = ?""",
        (submission_id,),
    ).fetchall()
    return db.content_hash(
        [f"{r['filename']}|{r['needs_ocr']}|{r['text']}" for r in rows]
    )


def cached(conn: sqlite3.Connection, submission_id: str) -> Optional[SubmissionExtract]:
    """Return the stored extract if it still matches the current packet."""
    row = conn.execute(
        "SELECT content_hash, payload_json FROM extracts WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    if row is None or row["content_hash"] != packet_hash(conn, submission_id):
        return None
    return SubmissionExtract.model_validate_json(row["payload_json"])


def store(
    conn: sqlite3.Connection,
    submission_id: str,
    extract: SubmissionExtract,
    model: str = "fixture",
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    conn.execute(
        """INSERT INTO extracts
             (submission_id, content_hash, payload_json, model, tokens_in, tokens_out)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(submission_id) DO UPDATE SET
             content_hash=excluded.content_hash, payload_json=excluded.payload_json,
             model=excluded.model, tokens_in=excluded.tokens_in,
             tokens_out=excluded.tokens_out, created_at=datetime('now')""",
        (submission_id, packet_hash(conn, submission_id),
         extract.model_dump_json(), model, tokens_in, tokens_out),
    )
    db.set_status(conn, submission_id, "extracted")


def save_fixture(conn: sqlite3.Connection, submission_id: str,
                 extract: SubmissionExtract) -> Path:
    """Record a real model response so the pipeline can be tested offline.

    The content hash travels with it, so a test can tell a stale fixture from
    a current one instead of silently asserting against yesterday's corpus.
    """
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / f"{submission_id}.json"
    path.write_text(json.dumps({
        "submission_id": submission_id,
        "content_hash": packet_hash(conn, submission_id),
        "extract": extract.model_dump(mode="json"),
    }, indent=2))
    return path


def load_fixture(submission_id: str) -> Optional[dict]:
    path = FIXTURES_DIR / f"{submission_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def extract_one(
    conn: sqlite3.Connection,
    submission_id: str,
    *,
    force: bool = False,
    record_fixture: bool = False,
) -> dict:
    """Extract one submission, using the cache unless forced."""
    if not force:
        hit = cached(conn, submission_id)
        if hit is not None:
            return {"submission_id": submission_id, "source": "cache",
                    "tokens": 0, "extract": hit}

    from intake.agent.crew import MODEL, ExtractionCrew
    from intake.agent import tools

    tools.bind(db.DB_PATH)
    result = ExtractionCrew().crew().kickoff(inputs={"submission_id": submission_id})
    extract = result.pydantic
    if extract is None:
        raise RuntimeError(f"{submission_id}: model returned no valid extract")

    usage = result.token_usage
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0

    store(conn, submission_id, extract, model=MODEL,
          tokens_in=tokens_in, tokens_out=tokens_out)
    db.log_event(conn, submission_id, "extracted",
                 {"model": MODEL, "tokens_in": tokens_in, "tokens_out": tokens_out,
                  "confidence": extract.extraction_confidence})
    if record_fixture:
        save_fixture(conn, submission_id, extract)

    return {"submission_id": submission_id, "source": "model",
            "tokens": tokens_in + tokens_out, "extract": extract}


def run(
    conn: sqlite3.Connection,
    *,
    only: Optional[list[str]] = None,
    limit: Optional[int] = None,
    force: bool = False,
    dry_run: bool = False,
    record_fixtures: bool = False,
    use_fixtures: bool = False,
) -> dict:
    """Extract every parsed submission.

    dry_run reports what would be called without calling anything.
    use_fixtures replays recorded responses, so the whole pipeline can be
    exercised end to end with no API access at all.
    """
    if only:
        ids = list(only)
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM submissions WHERE status IN ('parsed','extracted') "
            "ORDER BY id")]

    hits, misses = [], []
    for sid in ids:
        if not force and cached(conn, sid) is not None:
            hits.append(sid)
        else:
            misses.append(sid)

    if limit is not None:
        misses = misses[:limit]

    if dry_run:
        return {"cached": len(hits), "would_call": len(misses),
                "would_call_ids": misses, "extracted": 0, "tokens": 0, "failed": []}

    extracted, tokens, failed = [], 0, []

    if use_fixtures:
        for sid in misses:
            fixture = load_fixture(sid)
            if fixture is None:
                failed.append({"submission_id": sid, "error": "no fixture recorded"})
                continue
            # Provenance is kept so a synthesised extract is never mistaken for
            # a real model response, here or anywhere downstream.
            origin = "fixture:synthetic" if fixture.get("synthetic") else "fixture:model"
            store(conn, sid, SubmissionExtract.model_validate(fixture["extract"]),
                  model=origin)
            extracted.append(sid)
        return {"cached": len(hits), "extracted": len(extracted),
                "tokens": 0, "failed": failed, "source": "fixtures"}

    for sid in misses:
        try:
            result = extract_one(conn, sid, force=force, record_fixture=record_fixtures)
            extracted.append(sid)
            tokens += result["tokens"]
            conn.commit()  # bank each success; a later failure must not undo it
        except Exception as exc:
            failed.append({"submission_id": sid, "error": str(exc)[:200]})
            db.log_event(conn, sid, "extraction_failed", {"error": str(exc)[:500]})
            conn.commit()

    return {"cached": len(hits), "extracted": len(extracted),
            "tokens": tokens, "failed": failed, "source": "model"}
