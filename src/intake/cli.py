"""Pipeline CLI.

    python -m intake.cli ingest        read the mailbox, register submissions
    python -m intake.cli parse         extract text and classify documents
    python -m intake.cli extract       read the packets (the only LLM stage)
    python -m intake.cli clear         check for the same risk from two brokers
    python -m intake.cli assess        run the appetite guide
    python -m intake.cli score         rank the queue
    python -m intake.cli status        where everything currently sits
    python -m intake.cli show SUB-0001 one submission in detail

Stages are idempotent and re-runnable in isolation, so any of these can be
run again without undoing work.
"""

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from intake import db
from intake.pipeline import assess, clearance, extract, ingest, parse, score

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


def cmd_extract(args) -> None:
    with db.session() as conn:
        result = extract.run(
            conn, only=args.only, limit=args.limit, force=args.force,
            dry_run=args.dry_run, record_fixtures=args.record_fixtures,
            use_fixtures=args.use_fixtures,
        )

    if args.dry_run:
        print(f"cached, no call needed  {result['cached']}")
        print(f"would call the model    {result['would_call']}")
        if result["would_call_ids"]:
            ids = result["would_call_ids"]
            print("                        " + ", ".join(ids[:8])
                  + (f" ... (+{len(ids) - 8})" if len(ids) > 8 else ""))
        return

    print(f"served from cache       {result['cached']}")
    print(f"extracted               {result['extracted']}  ({result.get('source')})")
    if result["tokens"]:
        print(f"tokens                  {result['tokens']:,}")
    if result["failed"]:
        print(f"\nfailed ({len(result['failed'])}):")
        for f in result["failed"]:
            print(f"   {f['submission_id']}: {f['error']}")


def cmd_clear(args) -> None:
    with db.session() as conn:
        result = clearance.run(conn, only=args.only, rebuild=args.rebuild)

    print(f"cleared                 {result['cleared']}")
    print(f"duplicates              {len(result['duplicates'])}")
    print(f"flagged for review      {len(result['review'])}")

    for label, rows in (("duplicate of", result["duplicates"]),
                        ("possible match", result["review"])):
        for r in rows:
            broker = f" ({r.matched_broker} submitted first)" if r.matched_broker else ""
            print(f"\n  {r.submission_id}  {label} {r.matched_submission_id}"
                  f"  score {r.match_score}{broker}")
            print(f"    {r.match_basis}")

    if result["stale_extracts"]:
        print(f"\nskipped, extract no longer matches the packet "
              f"({len(result['stale_extracts'])}): "
              f"{', '.join(result['stale_extracts'])}")
    if result["not_extracted"]:
        print(f"\nskipped, not extracted yet: {', '.join(result['not_extracted'])}")


def _as_of(args) -> date:
    """Both later stages read the calendar, so the date is an argument.

    Defaulting to today is right in use and useless in a demo, where the
    corpus was generated against a fixed week. --as-of pins it.
    """
    if not getattr(args, "as_of", None):
        return date.today()
    try:
        return datetime.strptime(args.as_of, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"--as-of wants YYYY-MM-DD, got '{args.as_of}'")


def cmd_assess(args) -> None:
    with db.session() as conn:
        result = assess.run(conn, only=args.only, rebuild=args.rebuild,
                            as_of=_as_of(args))

    print(f"assessed                {result['assessed']}"
          f"  (guide {result['guide_version']}, as of {result['as_of']})\n")
    for outcome in ("accept", "refer", "pending_info", "decline"):
        if outcome in result["outcomes"]:
            print(f"  {outcome:<14} {result['outcomes'][outcome]}")
    if result["queues"]:
        print("\nqueues")
        for queue, n in sorted(result["queues"].items(), key=lambda kv: -kv[1]):
            print(f"  {queue:<24} {n}")
    if result["stale_extracts"]:
        print(f"\nskipped, extract no longer matches the packet: "
              f"{', '.join(result['stale_extracts'])}")
    if result["not_extracted"]:
        print(f"\nskipped, not extracted yet: {', '.join(result['not_extracted'])}")


def cmd_score(args) -> None:
    with db.session() as conn:
        result = score.run(conn, only=args.only, rebuild=args.rebuild,
                           as_of=_as_of(args))

    worklist = result["worklist"]
    print(f"scored                  {result['scored']}"
          f"  (weights {result['weights_version']}, as of {result['as_of']})")
    print(f"on the worklist         {len(worklist)}")
    print(f"held back               {len(result['suppressed'])}\n")

    for rank, s in enumerate(worklist[:args.top], start=1):
        parts = "  ".join(f"{c.name.split('_')[0]} {c.raw:.0f}" for c in s.components)
        print(f"  {rank:>3}. {s.submission_id}  {s.total:>5.1f}   {parts}")
    if len(worklist) > args.top:
        print(f"       ... {len(worklist) - args.top} more")

    if result["suppressed"]:
        print("\nheld back")
        for s in result["suppressed"]:
            print(f"  {s.submission_id}  {s.total:>5.1f}   {s.suppressed_because}")
    if result["not_assessed"]:
        print(f"\nskipped, not assessed yet: {', '.join(result['not_assessed'])}")
    if result["stale_extracts"]:
        print(f"\nskipped, extract no longer matches the packet: "
              f"{', '.join(result['stale_extracts'])}")


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
        provenance = conn.execute(
            "SELECT model, COUNT(*) n, SUM(tokens_in + tokens_out) t "
            "FROM extracts GROUP BY model ORDER BY n DESC"
        ).fetchall()
        clear = conn.execute(
            "SELECT SUM(is_duplicate) dupes, SUM(needs_review) review, "
            "COUNT(*) n FROM clearance"
        ).fetchone()
        decided = conn.execute(
            "SELECT outcome, COUNT(*) n FROM decisions GROUP BY outcome"
        ).fetchall()
        ranked = conn.execute(
            "SELECT COUNT(*) n, SUM(suppressed) held, AVG(total) avg, "
            "MAX(total) best FROM scores"
        ).fetchone()

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

    if provenance:
        print("\nextract provenance")
        for r in provenance:
            tok = f"  {r['t']:,} tokens" if r["t"] else ""
            print(f"  {r['model']:<28} {r['n']}{tok}")
        if any(r["model"] == "fixture:synthetic" for r in provenance):
            print("\n  ** fixture:synthetic extracts are scaffolding, not model")
            print("     output. Re-run `extract` to replace them as quota allows.")

    if clear and clear["n"]:
        print(f"\nclearance ({clear['n']} checked)")
        print(f"  {clear['dupes'] or 0} duplicate(s), "
              f"{clear['review'] or 0} flagged for review")

    if decided:
        print("\nappetite")
        order = {"accept": 0, "refer": 1, "pending_info": 2, "decline": 3}
        for r in sorted(decided, key=lambda r: order.get(r["outcome"], 9)):
            print(f"  {r['outcome']:<14} {r['n']}")

    if ranked and ranked["n"]:
        print(f"\nworklist ({ranked['n'] - (ranked['held'] or 0)} ranked, "
              f"{ranked['held'] or 0} held back)")
        print(f"  top score {ranked['best']:.1f}, average {ranked['avg']:.1f}")


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
        clear = conn.execute(
            """SELECT c.*, b.name first_broker
               FROM clearance c
               LEFT JOIN submissions m ON m.id = c.matched_submission_id
               LEFT JOIN brokers b ON b.id = m.broker_id
               WHERE c.submission_id = ?""", (args.submission_id,)).fetchone()
        decision = assess.load(conn, args.submission_id)
        ranked = conn.execute(
            "SELECT * FROM scores WHERE submission_id = ?",
            (args.submission_id,)).fetchone()

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

    if clear:
        verdict = ("DUPLICATE" if clear["is_duplicate"]
                   else "review" if clear["needs_review"] else "clear")
        print(f"\n  clearance  [{verdict}]")
        print(f"    normalised  {clear['normalised_name']} / "
              f"{clear['normalised_address'] or '-'}")
        if clear["matched_submission_id"]:
            broker = f", {clear['first_broker']}" if clear["first_broker"] else ""
            print(f"    matches     {clear['matched_submission_id']} "
                  f"(score {clear['match_score']}{broker})")
        print(f"    basis       {clear['match_basis']}")

    if decision:
        print(f"\n  appetite   [{decision['outcome'].upper()}]"
              f"   guide {decision['guide_version']}, as of {decision['as_of']}")
        print(f"    queue       {decision['queue'] or '-'}  "
              f"priority {decision['priority']}")
        for rule in decision["fired_rules"]:
            print(f"    {rule['rule_id']:<26} {rule['detail']}")
        for item in decision["information_required"]:
            print(f"    NEEDED                     {item}")

    if ranked:
        held = f"   HELD BACK: {ranked['suppressed_because']}" if ranked["suppressed"] else ""
        print(f"\n  score      {ranked['total']}{held}")
        print(f"    premium proxy  ~${ranked['estimated_premium_usd']:,.0f}"
              f"   (triage indication only, not a rate)")
        for c in json.loads(ranked["components_json"]):
            print(f"    {c['name']:<15} {c['raw']:>5.1f} x {c['weight']:.2f} "
                  f"= {c['contribution']:>5.2f}   {c['basis']}")

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

    s = sub.add_parser("extract", help="run the extraction agent (the only LLM stage)")
    s.add_argument("--only", nargs="*", help="specific submission ids")
    s.add_argument("--limit", type=int, help="cap how many model calls to make")
    s.add_argument("--force", action="store_true", help="ignore the cache")
    s.add_argument("--dry-run", action="store_true",
                   help="report what would be called, call nothing")
    s.add_argument("--record-fixtures", action="store_true",
                   help="save responses for offline replay")
    s.add_argument("--use-fixtures", action="store_true",
                   help="replay recorded responses instead of calling the model")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("clear", help="check for the same risk from two brokers")
    s.add_argument("--only", nargs="*", help="specific submission ids")
    s.add_argument("--rebuild", action="store_true",
                   help="discard prior clearance and re-run the whole mailbox")
    s.set_defaults(func=cmd_clear)

    s = sub.add_parser("assess", help="run the appetite guide over the extracts")
    s.add_argument("--only", nargs="*", help="specific submission ids")
    s.add_argument("--rebuild", action="store_true",
                   help="discard prior decisions and re-evaluate everything")
    s.add_argument("--as-of", dest="as_of", metavar="YYYY-MM-DD",
                   help="evaluate against this date instead of today")
    s.set_defaults(func=cmd_assess)

    s = sub.add_parser("score", help="rank the queue")
    s.add_argument("--only", nargs="*", help="specific submission ids")
    s.add_argument("--rebuild", action="store_true",
                   help="discard prior scores and re-rank everything")
    s.add_argument("--as-of", dest="as_of", metavar="YYYY-MM-DD",
                   help="score against this date instead of today")
    s.add_argument("--top", type=int, default=15, help="how many rows to print")
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("status", help="pipeline state summary")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("show", help="one submission in detail")
    s.add_argument("submission_id")
    s.set_defaults(func=cmd_show)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
