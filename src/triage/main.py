"""CLI entry point for submission extraction.

    python -m triage.main SUB-1001        extract one submission
    python -m triage.main --all           extract every submission in data/
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


def load_packet(submission_id: str) -> str:
    """Concatenate every document in a submission folder into one string."""
    folder = SUBMISSIONS_DIR / submission_id
    if not folder.is_dir():
        available = ", ".join(sorted(p.name for p in SUBMISSIONS_DIR.iterdir()))
        sys.exit(f"No submission '{submission_id}'. Available: {available}")

    parts = []
    for doc in sorted(folder.glob("*.txt")):
        parts.append(f"--- DOCUMENT: {doc.name} ---\n{doc.read_text()}")
    if not parts:
        sys.exit(f"Submission '{submission_id}' has no .txt documents in it.")
    return "\n\n".join(parts)


def run_one(submission_id: str) -> dict:
    from triage.crew import SubmissionTriageCrew

    packet = load_packet(submission_id)
    print(f"\n{'=' * 70}\n  EXTRACTING {submission_id}  ({len(packet):,} chars)\n{'=' * 70}")

    result = SubmissionTriageCrew().crew().kickoff(
        inputs={"submission_id": submission_id, "documents": packet}
    )

    extract = result.pydantic
    data = extract.model_dump()

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"{submission_id}.json"
    out_path.write_text(json.dumps(data, indent=2))

    summarise(submission_id, extract)
    print(f"\n  Full JSON: {out_path.relative_to(PROJECT_ROOT)}")
    return data


def summarise(submission_id: str, e) -> None:
    """Print the parts an underwriter would look at first."""
    money = lambda v: f"${v:,.0f}" if v is not None else "not stated"

    print(f"\n{'-' * 70}\n  {submission_id}: {e.insured.named_insured or 'UNKNOWN INSURED'}")
    print(f"{'-' * 70}")
    print(f"  Broker         {e.broker.agency_name or '?'} / {e.broker.producer_name or '?'}")
    print(f"  Operations     {(e.insured.business_description or 'not stated')[:60]}")
    print(f"  NAICS          {e.insured.naics_code or 'not stated'}")
    print(f"  Revenue        {money(e.insured.annual_revenue_usd)}")
    print(f"  Employees      {e.insured.employee_count if e.insured.employee_count is not None else 'not stated'}")

    tiv_flag = "  (ESTIMATED)" if e.tiv_is_estimated else ""
    print(f"  TIV            {money(e.total_insured_value_usd)}{tiv_flag}")
    print(f"  Locations      {len(e.locations)}")
    print(f"  Effective      {e.effective_date or 'not stated'}")

    lobs = ", ".join(c.line_of_business or "?" for c in e.coverages_requested) or "none stated"
    print(f"  Coverages      {lobs}")

    runs = "carrier loss runs attached" if e.loss_runs_provided else "NO FORMAL LOSS RUNS"
    print(f"  Losses         {money(e.total_incurred_5yr_usd)} over {len(e.loss_runs)} entries ({runs})")
    if e.prior_non_renewal:
        print("  ** PRIOR NON-RENEWAL **")

    print(f"\n  Confidence     {e.extraction_confidence:.2f}")
    if e.missing_fields:
        print(f"\n  Missing ({len(e.missing_fields)}):")
        for m in e.missing_fields:
            print(f"    - {m}")
    if e.ambiguities:
        print(f"\n  Ambiguities ({len(e.ambiguities)}):")
        for a in e.ambiguities:
            print(f"    - {a}")


def main() -> None:
    p = argparse.ArgumentParser(description="Extract underwriting facts from a submission packet")
    p.add_argument("submission_id", nargs="?", help="e.g. SUB-1001")
    p.add_argument("--all", action="store_true", help="run every submission")
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
