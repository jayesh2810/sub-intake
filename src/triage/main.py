"""CLI entry point for submission triage.

    python -m triage.main SUB-1001        triage one submission
    python -m triage.main --all           triage every submission in data/
    python -m triage.main --list          list available submissions
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUBMISSIONS_DIR = PROJECT_ROOT / "data" / "submissions"
OUTPUT_DIR = PROJECT_ROOT / "output"

OUTCOME_LABEL = {
    "accept": "ACCEPT",
    "refer": "REFER TO UNDERWRITER",
    "decline": "DECLINE",
    "pending_info": "PENDING INFORMATION",
}


def run_one(submission_id: str) -> dict:
    from triage.crew import SubmissionTriageCrew

    if not (SUBMISSIONS_DIR / submission_id).is_dir():
        available = ", ".join(sorted(p.name for p in SUBMISSIONS_DIR.iterdir() if p.is_dir()))
        sys.exit(f"No submission '{submission_id}'. Available: {available}")

    print(f"\n{'=' * 70}\n  TRIAGE {submission_id}\n{'=' * 70}")

    result = SubmissionTriageCrew().crew().kickoff(inputs={"submission_id": submission_id})

    extract = result.tasks_output[0].pydantic
    decision = result.tasks_output[1].pydantic

    payload = {
        "submission_id": submission_id,
        "decision": decision.model_dump(mode="json"),
        "extract": extract.model_dump(mode="json"),
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"{submission_id}.json"
    out_path.write_text(json.dumps(payload, indent=2))

    report(submission_id, extract, decision)
    print(f"\n  Saved: {out_path.relative_to(PROJECT_ROOT)}")
    return payload


def report(submission_id: str, e, d) -> None:
    money = lambda v: f"${v:,.0f}" if v is not None else "not stated"

    print(f"\n{'-' * 70}")
    print(f"  {submission_id}  {d.named_insured or e.insured.named_insured or 'UNKNOWN'}")
    print(f"{'-' * 70}")
    print(f"  Broker         {e.broker.agency_name or '?'} / {e.broker.producer_name or '?'}")
    if d.naics:
        print(f"  Class          {d.naics.code} {d.naics.title[:44]}")
        print(f"                 hazard grade {d.naics.hazard_grade}, matched on {d.naics.matched_on}")
    print(f"  Revenue        {money(e.insured.annual_revenue_usd)}")
    tiv_flag = "  (ESTIMATED)" if e.tiv_is_estimated else ""
    print(f"  TIV            {money(e.total_insured_value_usd)}{tiv_flag}")
    print(f"  Effective      {e.effective_date or 'not stated'}")
    runs = "carrier loss runs" if e.loss_runs_provided else "NO FORMAL LOSS RUNS"
    print(f"  Losses         {money(e.total_incurred_5yr_usd)} ({runs})")

    print(f"\n  >>> {OUTCOME_LABEL.get(d.outcome.value, d.outcome.value.upper())}")
    print(f"      queue {d.queue or '-'}   priority {d.priority or '-'}   "
          f"extraction confidence {e.extraction_confidence:.2f}")

    if d.fired_rules:
        print(f"\n  Rules fired ({len(d.fired_rules)}):")
        for r in d.fired_rules:
            print(f"    [{r.outcome.value:<12}] {r.rule_id}")
            print(f"                   {r.detail}")

    print(f"\n  Rationale:\n    {d.rationale}")

    if d.information_required:
        print("\n  Information required:")
        for item in d.information_required:
            print(f"    - {item}")

    if d.broker_message:
        print("\n  Draft reply to broker:")
        for line in d.broker_message.splitlines():
            print(f"    {line}")


def main() -> None:
    p = argparse.ArgumentParser(description="Triage a commercial insurance submission")
    p.add_argument("submission_id", nargs="?", help="e.g. SUB-1001")
    p.add_argument("--all", action="store_true", help="triage every submission")
    p.add_argument("--list", action="store_true", help="list available submissions")
    args = p.parse_args()

    ids = sorted(d.name for d in SUBMISSIONS_DIR.iterdir() if d.is_dir())

    if args.list:
        print("Available submissions:")
        for sid in ids:
            docs = ", ".join(sorted(f.name for f in (SUBMISSIONS_DIR / sid).glob("*.txt")))
            print(f"  {sid}  [{docs}]")
        return

    if args.all:
        for sid in ids:
            run_one(sid)
        return

    if not args.submission_id:
        p.error("give a submission id, or use --all / --list")

    run_one(args.submission_id)


if __name__ == "__main__":
    main()
