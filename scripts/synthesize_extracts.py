"""Development scaffolding - NOT the extractor.

Builds SubmissionExtract fixtures by parsing the synthetic corpus with regex.
It works only because we generated those documents ourselves and know their
exact layout. It would fall apart on a real broker submission, which is the
entire reason the real extractor is a model.

Why it exists: free tier Gemini caps requests per day, so extracting 50
submissions takes several days. Stages D through G need 50 extracts to be
built and tested against. This produces them offline, deterministically, at
no cost.

Fixtures produced here are stored with `model = "fixture:synthetic"`, so a
synthesised extract is always distinguishable from a real model response in
the database, in the CLI, and on the worklist. Replace them by running the
real extractor as quota allows - the cache will take the model's answer.

    python scripts/synthesize_extracts.py            # all missing
    python scripts/synthesize_extracts.py --force    # overwrite all
"""

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from intake import db  # noqa: E402
from intake.pipeline import extract as extract_stage  # noqa: E402

MONEY = r"\$?\s*([\d,]+)"


def _num(text, pattern, cast=float):
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    raw = m.group(1).replace(",", "").strip()
    try:
        return cast(raw)
    except ValueError:
        return None


def _str(text, pattern):
    m = re.search(pattern, text, re.I)
    return m.group(1).strip() if m else None


def _date(text, pattern):
    raw = _str(text, pattern)
    if not raw:
        return None
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw)
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None


def parse_acord(t: str) -> dict:
    out = {
        "named_insured": _str(t, r"NAME \(First Named Insured\):\s*(.+)"),
        "mailing_address": _str(t, r"MAILING ADDRESS:\s*(.+)"),
        "years_in_business": _num(t, r"YEARS IN BUSINESS:\s*(\d+)", int),
        "business_description": _str(t, r"DESCRIPTION OF OPERATIONS:\s*(.+)"),
        "annual_revenue_usd": _num(t, rf"ANNUAL REVENUE:\s*{MONEY}"),
        "employee_count": _num(t, r"FULL TIME EMPLOYEES:\s*(\d+)", int),
        "deductible_usd": _num(t, rf"DEDUCTIBLE\s+{MONEY}"),
        "effective_date": _date(t, r"PROPOSED EFFECTIVE DATE:\s*(\S+)"),
        "expiration_date": _date(t, r"EXPIRATION DATE:\s*(\S+)"),
        # (?m) or this anchor only matches the top of the document, and the
        # carrier line sits well down the form. Without it every submission
        # looks like the incumbent was never disclosed.
        "incumbent_carrier": _str(t, r"(?m)^CARRIER:\s*(.+)$"),
        "expiring_premium_usd": _num(t, rf"ANNUAL PREMIUM:\s*{MONEY}"),
        "reason_for_shopping": _str(t, r"REASON FOR SHOPPING:\s*(.+)"),
    }
    naics = _str(t, r"NAICS:\s*(\d{6})")
    out["naics_code"] = naics
    # The city/state/zip sits on the line after MAILING ADDRESS. Anchors need
    # MULTILINE here, and the state must stay case sensitive.
    m = re.search(
        r"MAILING ADDRESS:.*\n\s*([A-Za-z .'-]+?),\s*([A-Z]{2})\s+(\d{5})", t
    )
    if m:
        out.update(city=m.group(1).strip(), state=m.group(2), zip_code=m.group(3))
    out["lines_of_business"] = re.findall(r"\[X\]\s+(COMMERCIAL [A-Z ]+)", t)
    if out["incumbent_carrier"] and "not disclosed" in out["incumbent_carrier"].lower():
        out["incumbent_carrier"] = None
    return out


def parse_sov(t: str) -> dict:
    locations = []
    for block in re.split(r"\nLOC \d+ -- ", "\n" + t)[1:]:
        head = block.splitlines()[0]
        addr, *rest = head.split(",")
        loc = {
            "address": addr.strip(),
            "city": rest[0].strip() if rest else None,
            "construction_type": _str(block, r"Construction:\s*(.+)"),
            "year_built": _num(block, r"Year Built:\s*(\d{4})", int),
            "square_feet": _num(block, r"Square Footage:\s*([\d,]+)", int),
            "building_value_usd": _num(block, rf"Building\s+{MONEY}"),
            "contents_value_usd": _num(block, rf"Contents / Equipment\s+{MONEY}"),
            "business_income_usd": _num(block, rf"Business Income\s+{MONEY}"),
        }
        spr = _str(block, r"Sprinklered:\s*(\w+)")
        loc["sprinklered"] = None if spr is None else spr.lower() == "yes"
        if rest and len(rest) > 1:
            sz = rest[1].strip().split()
            if len(sz) == 2:
                loc["state"], loc["zip_code"] = sz
        locations.append(loc)

    tiv = _num(t, rf"TOTAL INSURABLE VALUE\s+approx\s+{MONEY}")
    estimated = tiv is not None
    if tiv is None:
        tiv = _num(t, rf"TOTAL INSURABLE VALUE\s+{MONEY}")
    return {"locations": locations, "total_insured_value_usd": tiv,
            "tiv_is_estimated": estimated}


def parse_loss_run(t: str) -> dict:
    rows = []
    for m in re.finditer(
        r"^(\d{4}-\d{4})\s+(PROP|GL|AUTO)\s+(\d+)\s+\$\s*([\d,]+)\s+\$\s*([\d,]+)",
        t, re.M,
    ):
        rows.append({
            "policy_year": m.group(1),
            "line_of_business": {"PROP": "Property", "GL": "General Liability",
                                 "AUTO": "Auto"}[m.group(2)],
            "claim_count": int(m.group(3)),
            "paid_usd": float(m.group(4).replace(",", "")),
            "incurred_usd": float(m.group(5).replace(",", "")),
        })
    return {
        "loss_runs": rows,
        "total_incurred_5yr_usd": sum(r["incurred_usd"] for r in rows) or None,
        "open_claims": _num(t, r"OPEN CLAIMS:\s*(\d+)", int),
        "prior_non_renewal": bool(re.search(r"non-renewal issued", t, re.I)),
    }


def parse_email(t: str) -> dict:
    sender = _str(t, r"From:\s*(.+?)\s*<")
    agency = None
    lines = [l.strip() for l in t.strip().splitlines() if l.strip()]
    if sender and lines:
        for i, l in enumerate(lines):
            if l == sender and i + 1 < len(lines):
                agency = lines[i + 1]
    return {"producer_name": sender, "agency_name": agency,
            "email": _str(t, r"From:.*<(.+?)>"),
            "submitted_date": None}


def build(conn, submission_id: str) -> dict:
    docs = {
        r["doc_type"]: r
        for r in conn.execute(
            "SELECT doc_type, text, needs_ocr FROM documents WHERE submission_id=?",
            (submission_id,))
    }
    acord = parse_acord(docs["acord_125"]["text"]) if "acord_125" in docs else {}
    sov = parse_sov(docs["sov"]["text"]) if docs.get("sov") and not docs["sov"]["needs_ocr"] else {}
    email = parse_email(docs["email_body"]["text"]) if "email_body" in docs else {}

    lr_doc = docs.get("loss_run")
    readable_runs = bool(lr_doc) and not lr_doc["needs_ocr"]
    lr = parse_loss_run(lr_doc["text"]) if readable_runs else {
        "loss_runs": [], "total_incurred_5yr_usd": None, "open_claims": None,
        "prior_non_renewal": False}

    missing, ambiguities = [], []
    if not acord.get("naics_code"):
        missing.append("NAICS code")
    if acord.get("annual_revenue_usd") is None:
        missing.append("annual revenue")
    if not readable_runs:
        missing.append("carrier loss runs")
        if lr_doc is not None:
            ambiguities.append(
                "Loss run document could not be read; loss history is unknown, "
                "not zero.")
    if sov.get("tiv_is_estimated"):
        ambiguities.append("Total insured value is an owner estimate, appraisal pending.")
    if not sov:
        missing.append("statement of values")
    if lr.get("prior_non_renewal"):
        ambiguities.append("Prior carrier issued a non-renewal for adverse losses.")

    confidence = 0.9
    confidence -= 0.25 if not readable_runs else 0.0
    confidence -= 0.15 if sov.get("tiv_is_estimated") else 0.0
    confidence -= 0.10 if acord.get("annual_revenue_usd") is None else 0.0
    confidence -= 0.05 if not acord.get("naics_code") else 0.0

    return {
        "broker": {"producer_name": email.get("producer_name"),
                   "agency_name": email.get("agency_name"),
                   "email": email.get("email"), "phone": None,
                   "submitted_date": None},
        "insured": {
            "named_insured": acord.get("named_insured"), "dba": None,
            "entity_type": None, "mailing_address": acord.get("mailing_address"),
            "city": acord.get("city"), "state": acord.get("state"),
            "zip_code": acord.get("zip_code"), "website": None,
            "business_description": acord.get("business_description"),
            "naics_code": acord.get("naics_code"), "sic_code": None,
            "years_in_business": acord.get("years_in_business"),
            "annual_revenue_usd": acord.get("annual_revenue_usd"),
            "employee_count": acord.get("employee_count"),
        },
        "coverages_requested": [
            {"line_of_business": lob, "limit_occurrence_usd": 1_000_000,
             "limit_aggregate_usd": 2_000_000,
             "deductible_usd": acord.get("deductible_usd"), "notes": None}
            for lob in acord.get("lines_of_business", [])
        ],
        "effective_date": acord.get("effective_date"),
        "expiration_date": acord.get("expiration_date"),
        "locations": sov.get("locations", []),
        "total_insured_value_usd": sov.get("total_insured_value_usd"),
        "tiv_is_estimated": sov.get("tiv_is_estimated"),
        "loss_runs": lr["loss_runs"], "notable_claims": [],
        "total_incurred_5yr_usd": lr["total_incurred_5yr_usd"],
        "open_claims": lr["open_claims"],
        "loss_runs_provided": readable_runs,
        "incumbent_carrier": acord.get("incumbent_carrier"),
        "expiring_premium_usd": acord.get("expiring_premium_usd"),
        "reason_for_shopping": acord.get("reason_for_shopping"),
        "prior_non_renewal": lr["prior_non_renewal"],
        "missing_fields": missing, "ambiguities": ambiguities,
        "extraction_confidence": round(max(0.05, min(1.0, confidence)), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true",
                    help="regenerate synthetic fixtures (real ones are kept)")
    ap.add_argument("--overwrite-real", action="store_true",
                    help="also destroy recorded model responses - rarely what you want")
    args = ap.parse_args()

    written, skipped, real = 0, 0, []
    with db.session() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM submissions WHERE status IN "
            "('parsed','extracted','cleared','assessed','scored','queued') ORDER BY id")]
        for sid in ids:
            path = extract_stage.FIXTURES_DIR / f"{sid}.json"
            if path.is_file():
                existing = json.loads(path.read_text())
                # A real model response is expensive and slow to reproduce.
                # --force regenerates synthetic fixtures; it does not destroy
                # real ones. That needs saying explicitly.
                if not existing.get("synthetic") and not args.overwrite_real:
                    real.append(sid)
                    skipped += 1
                    continue
                if existing.get("synthetic") and not args.force:
                    skipped += 1
                    continue
            payload = build(conn, sid)
            extract_stage.FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "submission_id": sid,
                "content_hash": extract_stage.packet_hash(conn, sid),
                "synthetic": True,
                "extract": payload,
            }, indent=2))
            written += 1

    print(f"synthesised {written} fixture(s)")
    if real:
        print(f"kept {len(real)} real model fixture(s): {', '.join(real)}")
    print("\nThese are scaffolding, not extraction. Load them with:")
    print("  python -m intake.cli extract --use-fixtures")


if __name__ == "__main__":
    main()
