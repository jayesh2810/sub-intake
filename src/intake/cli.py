"""Pipeline CLI.

    python -m intake.cli ingest        read the mailbox, register submissions
    python -m intake.cli parse         extract text and classify documents
    python -m intake.cli status        where everything currently sits
    python -m intake.cli show SUB-0001 one submission in detail

Stages are idempotent and re-runnable in isolation, so any of these can be
run again without undoing work.
"""

import argparse
from pathlib import Path

from dotenv import load_dotenv

from intake import db
from intake.pipeline import ingest, parse

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def cmd_ingest(args) -> None:
    with db.session() as conn:
        result = ingest.run(conn, mailbox=Path(args.mailbox) if args.mailbox else None)
    print(f"brokers loaded     {result['brokers']}")
    print(f"newly ingested     {result['ingested']}")
    print(f"already present    {result['already_present']}")
    if result["ingested"]:
        ids = result["new_ids"]
        preview = ", ".join(ids[:6]) + (f" ... {ids[-1]}" if len(ids) > 6 else "")
        print(f"                   {preview}")


def cmd_parse(args) -> None:
    with db.session() as conn:
        result = parse.run(conn, only=args.only)
    print(f"submissions parsed {result['parsed']}")
    print(f"documents          {result['documents']}")
    if result["needs_ocr"]:
        print(f"\nunreadable, flagged for manual handling ({len(result['needs_ocr'])}):")
        for sid in result["needs_ocr"]:
            print(f"   {sid}")


def cmd_status(args) -> None:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM submissions GROUP BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) n FROM submissions").fetchone()["n"]
        docs = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
        ocr = conn.execute(
            "SELECT COUNT(DISTINCT submission_id) n FROM documents WHERE needs_ocr = 1"
        ).fetchone()["n"]
        by_type = conn.execute(
            "SELECT doc_type, COUNT(*) n FROM documents GROUP BY doc_type ORDER BY n DESC"
        ).fetchall()

    if not total:
        print("No submissions. Run: python -m intake.cli ingest")
        return

    print(f"{total} submissions, {docs} documents\n")
    order = {s: i for i, s in enumerate(db.ALL_STATES)}
    for r in sorted(rows, key=lambda r: order.get(r["status"], 99)):
        print(f"  {r['status']:<14} {r['n']}")
    if ocr:
        print(f"\n  {ocr} submission(s) have unreadable documents")
    if by_type:
        print("\ndocument types")
        for r in by_type:
            print(f"  {r['doc_type']:<14} {r['n']}")


def cmd_show(args) -> None:
    with db.session() as conn:
        sub = conn.execute(
            """SELECT s.*, b.name broker_name, b.agency
               FROM submissions s LEFT JOIN brokers b ON b.id = s.broker_id
               WHERE s.id = ?""", (args.submission_id,)).fetchone()
        if sub is None:
            raise SystemExit(f"No submission '{args.submission_id}'")
        docs = conn.execute(
            """SELECT filename, doc_type, page_count, needs_ocr, extraction_method,
                      length(text) chars
               FROM documents WHERE submission_id = ? ORDER BY filename""",
            (args.submission_id,)).fetchall()
        events = conn.execute(
            "SELECT event_type, detail, actor, created_at FROM events "
            "WHERE submission_id = ? ORDER BY id", (args.submission_id,)).fetchall()

    print(f"{sub['id']}  [{sub['status']}]")
    print(f"  subject   {sub['subject']}")
    print(f"  received  {sub['received_at']}")
    print(f"  broker    {sub['broker_name'] or 'UNMATCHED'} / {sub['agency'] or '-'}")
    print(f"  packet    {sub['packet_dir']}")

    if docs:
        print(f"\n  documents ({len(docs)})")
        for d in docs:
            flag = "  ** UNREADABLE **" if d["needs_ocr"] else ""
            print(f"    {d['filename']:<26} {d['doc_type']:<14} "
                  f"{d['page_count']}p  {d['chars'] or 0:>6} chars{flag}")

    if events:
        print(f"\n  events ({len(events)})")
        for e in events:
            detail = (e["detail"] or "")[:70]
            print(f"    {e['created_at']}  {e['event_type']:<22} {detail}")


def main() -> None:
    p = argparse.ArgumentParser(prog="intake", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest", help="read the mailbox and register submissions")
    s.add_argument("--mailbox", help="override the mailbox directory")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("parse", help="extract text and classify documents")
    s.add_argument("--only", nargs="*", help="specific submission ids")
    s.set_defaults(func=cmd_parse)

    s = sub.add_parser("status", help="pipeline state summary")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("show", help="one submission in detail")
    s.add_argument("submission_id")
    s.set_defaults(func=cmd_show)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
