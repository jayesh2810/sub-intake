"""Stage 5 - assess. Run the appetite guide against what was extracted.

The rules engine in `appetite.py` is the whole of the decision. This stage is
the wiring: it maps a `SubmissionExtract` onto the engine's arguments, stores
the outcome with every rule that fired, and does nothing else. Anything that
looks like judgement belongs in `rules/appetite.yaml`, where an underwriter
can read it and argue with it.

Two facts have to travel with the answer. The guide version, because a risk
declined under the July guide is not the same as one declined under the
August guide. And the date it was evaluated against, because the tight
effective date rule reads the calendar - the same submission assessed a
fortnight later is legitimately a different answer, and a stored outcome with
no date is not reproducible.
"""

import json
import sqlite3
from datetime import date
from typing import Optional

from intake import appetite, db
from intake.models import AppetiteResult, SubmissionExtract
from intake.pipeline import extract as extract_stage


def _lines_of_business(ex: SubmissionExtract) -> list[str]:
    return [c.line_of_business for c in ex.coverages_requested if c.line_of_business]


def _risk_state(ex: SubmissionExtract) -> Optional[str]:
    """Where the exposure is, preferring a location over the mailing address.

    A holding company's mail goes wherever the accountant is. Licensing
    follows the building.
    """
    for loc in ex.locations:
        if loc.state:
            return loc.state
    return ex.insured.state


def _open_claims(ex: SubmissionExtract) -> Optional[int]:
    """Open claims across the loss runs, however the packet reported them.

    Left as None rather than 0 when nothing says: no evidence of open claims
    is not evidence of none, and the guide should not refer or clear on a
    number we invented.
    """
    if ex.open_claims is not None:
        return ex.open_claims
    per_year = [r.open_claims for r in ex.loss_runs if r.open_claims is not None]
    if per_year:
        return sum(per_year)
    flagged = [c for c in ex.notable_claims if c.is_open]
    return len(flagged) if flagged else None


def assess_one(ex: SubmissionExtract, as_of: Optional[date] = None) -> AppetiteResult:
    """Evaluate one extract against the guide. No database, no side effects."""
    return appetite.evaluate(
        naics_code=ex.insured.naics_code,
        business_description=ex.insured.business_description,
        state=_risk_state(ex),
        lines_of_business=_lines_of_business(ex),
        total_insured_value_usd=ex.total_insured_value_usd,
        tiv_is_estimated=ex.tiv_is_estimated,
        annual_revenue_usd=ex.insured.annual_revenue_usd,
        total_incurred_5yr_usd=ex.total_incurred_5yr_usd,
        open_claims=_open_claims(ex),
        prior_non_renewal=ex.prior_non_renewal,
        loss_runs_provided=ex.loss_runs_provided,
        years_in_business=ex.insured.years_in_business,
        effective_date=ex.effective_date,
        as_of=as_of,
    )


def store(
    conn: sqlite3.Connection,
    submission_id: str,
    result: AppetiteResult,
    as_of: date,
) -> None:
    conn.execute(
        """INSERT INTO decisions
             (submission_id, outcome, fired_rules_json, information_required_json,
              blocking_json, queue, priority, guide_version, as_of)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(submission_id) DO UPDATE SET
             outcome=excluded.outcome,
             fired_rules_json=excluded.fired_rules_json,
             information_required_json=excluded.information_required_json,
             blocking_json=excluded.blocking_json,
             queue=excluded.queue, priority=excluded.priority,
             guide_version=excluded.guide_version, as_of=excluded.as_of,
             created_at=datetime('now')""",
        (submission_id, result.outcome.value,
         json.dumps([r.model_dump(mode="json") for r in result.fired_rules]),
         json.dumps(result.information_required), json.dumps(result.blocking),
         result.queue, result.priority, result.guide_version, as_of.isoformat()),
    )
    db.log_event(
        conn, submission_id, "assessed",
        {"outcome": result.outcome.value,
         "rules": [r.rule_id for r in result.fired_rules],
         "queue": result.queue, "priority": result.priority,
         "guide_version": result.guide_version, "as_of": as_of.isoformat()},
    )
    db.set_status(conn, submission_id, "assessed")


def load(conn: sqlite3.Connection, submission_id: str) -> Optional[dict]:
    """The stored decision as plain values, for the stages that read it."""
    row = conn.execute(
        "SELECT * FROM decisions WHERE submission_id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["fired_rules"] = json.loads(out.pop("fired_rules_json") or "[]")
    out["information_required"] = json.loads(
        out.pop("information_required_json") or "[]")
    out["blocking"] = json.loads(out.pop("blocking_json") or "[]")
    return out


def run(
    conn: sqlite3.Connection,
    *,
    only: Optional[list[str]] = None,
    rebuild: bool = False,
    as_of: Optional[date] = None,
) -> dict:
    """Assess every submission that has a current extract.

    Costs nothing to re-run, which is the point: the guide is meant to be
    edited and the whole mailbox re-evaluated against it in a second.
    """
    when = as_of or date.today()
    if rebuild:
        conn.execute("DELETE FROM decisions")

    ids = list(only) if only else [
        r["id"] for r in conn.execute(
            "SELECT s.id FROM submissions s WHERE EXISTS "
            "(SELECT 1 FROM extracts e WHERE e.submission_id = s.id) "
            "ORDER BY s.id")
    ]

    outcomes: dict[str, int] = {}
    queues: dict[str, int] = {}
    assessed, stale, missing = 0, [], []
    for sid in ids:
        current = extract_stage.cached(conn, sid)
        if current is None:
            row = conn.execute(
                "SELECT 1 FROM extracts WHERE submission_id = ?", (sid,)).fetchone()
            (stale if row else missing).append(sid)
            continue
        result = assess_one(current, when)
        store(conn, sid, result, when)
        assessed += 1
        outcomes[result.outcome.value] = outcomes.get(result.outcome.value, 0) + 1
        if result.queue:
            queues[result.queue] = queues.get(result.queue, 0) + 1

    return {"assessed": assessed, "outcomes": outcomes, "queues": queues,
            "stale_extracts": stale, "not_extracted": missing,
            "as_of": when.isoformat(),
            "guide_version": appetite.guide().get("guide_version")}
