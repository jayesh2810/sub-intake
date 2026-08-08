"""Stage 6 - score. Where a submission sits in the queue, and why.

A weighted sum of five components, each 0-100, with the weights in
`rules/scoring.yaml`. Deliberately not a model: the first question a carrier
asks about a ranked list is "why is that one at the top", and the only
acceptable answer is a decomposition they can check by hand. Every component
carries the values it was computed from, so a total can always be taken back
apart.

The premium figure here is a triage proxy. It exists to separate a $9,000
account from a $200,000 one well enough to sort a queue, and it is labelled
as a proxy everywhere it surfaces. It is not a rate and must never be shown
to a broker.

Two things are scored but kept off the worklist: risks outside appetite, and
risks that belong to the broker who submitted them first. Both stay in the
database with their full breakdown - a decline that scored well on everything
except appetite is exactly the evidence you want when asking whether the
guide is drawn too tight.
"""

import json
import math
import sqlite3
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from intake import db, naics
from intake.models import ScoreComponent, SubmissionExtract, TriageScore
from intake.pipeline import extract as extract_stage

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"


@lru_cache(maxsize=1)
def weights() -> dict:
    return yaml.safe_load((RULES_DIR / "scoring.yaml").read_text())


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


# --- component 1: appetite fit ----------------------------------------------


def appetite_fit(outcome: Optional[str], cfg: Optional[dict] = None) -> tuple[float, str]:
    c = (cfg or weights())["appetite_fit"]
    if outcome is None:
        return 0.0, "not assessed"
    return float(c.get(outcome, 0)), f"appetite outcome {outcome}"


# --- component 2: premium value ---------------------------------------------


def premium_proxy(
    *,
    total_insured_value_usd: Optional[float],
    annual_revenue_usd: Optional[float],
    hazard_grade: Optional[int],
    lines_of_business: Optional[list[str]] = None,
    expiring_premium_usd: Optional[float] = None,
    cfg: Optional[dict] = None,
) -> tuple[float, str]:
    """A rough annual premium, used only to sort. Returns (usd, how)."""
    conf = cfg or weights()
    p = conf["premium_proxy"]

    if conf["premium_value"].get("prefer_expiring_premium") and expiring_premium_usd:
        return float(expiring_premium_usd), (
            f"expiring premium ${expiring_premium_usd:,.0f} as stated by the broker")

    grade = hazard_grade or p["default_hazard_grade"]
    graded = "" if hazard_grade else " (class unresolved, middle grade assumed)"

    lobs = " ".join(lines_of_business or []).lower()
    wants_property = not lobs or "propert" in lobs
    wants_liability = not lobs or "liabilit" in lobs

    parts, total = [], 0.0
    if wants_property and total_insured_value_usd:
        amount = total_insured_value_usd / 100 * p["property_rate_per_100_tiv"][grade]
        total += amount
        parts.append(f"TIV ${total_insured_value_usd:,.0f} at grade {grade}")
    if wants_liability and annual_revenue_usd:
        amount = annual_revenue_usd / 1000 * p["gl_rate_per_1000_revenue"][grade]
        total += amount
        parts.append(f"revenue ${annual_revenue_usd:,.0f} at grade {grade}")

    if not parts:
        # No exposure figures at all. A flat placeholder keeps the submission
        # in the middle of the list rather than letting missing data read as a
        # small account and sink it out of sight.
        return float(p["unknown_exposure_usd"]), "no exposure stated, placeholder used"

    return total, "proxy from " + " and ".join(parts) + graded


def premium_value(premium_usd: float, cfg: Optional[dict] = None) -> tuple[float, str]:
    """Log-scaled between the floor and ceiling in the rules.

    Log rather than linear because the gap that matters is between $5k and
    $50k, not between $400k and $450k.
    """
    c = (cfg or weights())["premium_value"]
    floor, ceiling = float(c["floor_usd"]), float(c["ceiling_usd"])
    if premium_usd <= floor:
        raw = 0.0
    elif premium_usd >= ceiling:
        raw = 100.0
    else:
        raw = 100 * math.log(premium_usd / floor) / math.log(ceiling / floor)
    return _clamp(raw), f"${premium_usd:,.0f} indicated"


# --- component 3: winnability -----------------------------------------------


def classify_shopping_reason(text: Optional[str], cfg: Optional[dict] = None) -> str:
    """Bucket the broker's prose reason for shopping. Keywords, not a model."""
    c = (cfg or weights())["winnability"]
    lowered = (text or "").lower()
    if not lowered.strip():
        return "unknown"
    for category, keywords in c["shopping_reason_keywords"].items():
        if any(str(k).lower() in lowered for k in keywords):
            return category
    return "unknown"


def _blended_hit_ratio(submissions_sent: int, bound: int, c: dict) -> float:
    """Observed hit ratio pulled toward the no-history default by sample size.

    A broker with four binds from twelve submissions has not demonstrated a
    33% hit ratio. Weighting the observation against a prior worth
    `shrinkage_prior_submissions` keeps a lucky start from outranking a long
    record, and returns the default exactly when there is no history at all.
    """
    prior_n = float(c["shrinkage_prior_submissions"])
    prior_ratio = (c["no_history_default"] / 100.0) * c["hit_ratio_at_100"]
    return ((bound + prior_n * prior_ratio) / (submissions_sent + prior_n)
            if submissions_sent + prior_n else prior_ratio)


def winnability(
    *,
    submissions_sent: Optional[int],
    bound: Optional[int],
    reason_for_shopping: Optional[str] = None,
    incumbent_known: bool = False,
    cfg: Optional[dict] = None,
) -> tuple[float, str]:
    c = (cfg or weights())["winnability"]
    sent, won = int(submissions_sent or 0), int(bound or 0)

    ratio = _blended_hit_ratio(sent, won, c)
    base = _clamp(100 * ratio / c["hit_ratio_at_100"])
    if sent:
        why = [f"broker {won}/{sent} bound, blended {ratio:.0%}"]
    else:
        why = ["no history with this broker"]

    category = classify_shopping_reason(reason_for_shopping, cfg)
    adjustment = float(c["shopping_reason_adjustment"].get(category, 0))
    if adjustment:
        why.append(f"shopping on {category} {adjustment:+.0f}")

    bonus = float(c["incumbent_known_bonus"]) if incumbent_known else 0.0
    if bonus:
        why.append(f"incumbent named {bonus:+.0f}")

    return _clamp(base + adjustment + bonus), ", ".join(why)


# --- component 4: urgency ---------------------------------------------------


def days_to_effective(effective_date: Optional[str], as_of: date) -> Optional[int]:
    if not effective_date:
        return None
    try:
        eff = datetime.strptime(effective_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (eff - as_of).days


def urgency(days: Optional[int], cfg: Optional[dict] = None) -> tuple[float, str]:
    c = (cfg or weights())["urgency"]
    if days is None:
        return float(c["unknown_score"]), "no effective date stated"
    if days < 0:
        # Already incepted elsewhere or about to be. Either way it is the
        # submission most likely to have been missed.
        return float(c["lapsed_score"]), f"effective date passed {-days} days ago"
    for band in c["bands"]:
        if days <= band["within_days"]:
            return float(band["score"]), f"{days} days to effective date"
    return float(c["beyond_score"]), f"{days} days to effective date"


# --- component 5: completeness ----------------------------------------------


def completeness(
    *,
    extraction_confidence: Optional[float],
    loss_runs_provided: Optional[bool],
    tiv_is_estimated: Optional[bool],
    needs_ocr: bool = False,
    cfg: Optional[dict] = None,
) -> tuple[float, str]:
    c = (cfg or weights())["completeness"]
    raw = float(extraction_confidence or 0.0) * 100
    why = [f"extraction confidence {raw:.0f}"]

    for flag, key, label in (
        (not loss_runs_provided, "loss_runs_missing_penalty", "no carrier loss runs"),
        (bool(tiv_is_estimated), "estimated_values_penalty", "values are estimates"),
        (needs_ocr, "needs_ocr_penalty", "unreadable document"),
    ):
        if flag:
            raw -= float(c[key])
            why.append(f"{label} -{c[key]}")

    return _clamp(raw), ", ".join(why)


# --- the score ---------------------------------------------------------------


def score_one(
    submission_id: str,
    ex: SubmissionExtract,
    *,
    outcome: Optional[str],
    hazard_grade: Optional[int] = None,
    submissions_sent: Optional[int] = None,
    bound: Optional[int] = None,
    needs_ocr: bool = False,
    is_duplicate: bool = False,
    as_of: Optional[date] = None,
    cfg: Optional[dict] = None,
) -> TriageScore:
    """Score one submission. No database, no side effects, no model."""
    conf = cfg or weights()
    when = as_of or date.today()
    w = conf["weights"]

    premium, premium_basis = premium_proxy(
        total_insured_value_usd=ex.total_insured_value_usd,
        annual_revenue_usd=ex.insured.annual_revenue_usd,
        hazard_grade=hazard_grade,
        lines_of_business=[c.line_of_business for c in ex.coverages_requested
                           if c.line_of_business],
        expiring_premium_usd=ex.expiring_premium_usd,
        cfg=conf,
    )
    value_raw, value_basis = premium_value(premium, conf)

    raws = {
        "appetite_fit": appetite_fit(outcome, conf),
        "premium_value": (value_raw, f"{value_basis} - {premium_basis}"),
        "winnability": winnability(
            submissions_sent=submissions_sent, bound=bound,
            reason_for_shopping=ex.reason_for_shopping,
            incumbent_known=bool(ex.incumbent_carrier), cfg=conf),
        "urgency": urgency(days_to_effective(ex.effective_date, when), conf),
        "completeness": completeness(
            extraction_confidence=ex.extraction_confidence,
            loss_runs_provided=ex.loss_runs_provided,
            tiv_is_estimated=ex.tiv_is_estimated,
            needs_ocr=needs_ocr, cfg=conf),
    }

    # Contribution is computed from the rounded raw, not the full-precision
    # one. The breakdown is published; a reader who multiplies the printed
    # numbers has to get the printed answer.
    components = [
        ScoreComponent(name=name, raw=round(raw, 1), weight=w[name],
                       contribution=round(round(raw, 1) * w[name], 2), basis=basis)
        for name, (raw, basis) in raws.items()
    ]
    total = round(sum(c.contribution for c in components), 1)

    reason = None
    if conf.get("suppress_declines") and outcome == "decline":
        reason = "outside appetite"
    elif conf.get("suppress_duplicates") and is_duplicate:
        reason = "already with the broker who submitted first"

    return TriageScore(
        submission_id=submission_id,
        # The total stays honest even when suppressed. Zeroing it would throw
        # away the fact that a declined risk was otherwise attractive, which
        # is the evidence for whether the guide is drawn in the right place.
        total=_clamp(total),
        components=components,
        estimated_premium_usd=round(premium, 2),
        suppressed=reason is not None,
        suppressed_because=reason,
        weights_version=conf.get("weights_version"),
        as_of=when.isoformat(),
    )


def store(conn: sqlite3.Connection, result: TriageScore) -> None:
    conn.execute(
        """INSERT INTO scores
             (submission_id, total, components_json, estimated_premium_usd,
              suppressed, suppressed_because, weights_version, as_of)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(submission_id) DO UPDATE SET
             total=excluded.total, components_json=excluded.components_json,
             estimated_premium_usd=excluded.estimated_premium_usd,
             suppressed=excluded.suppressed,
             suppressed_because=excluded.suppressed_because,
             weights_version=excluded.weights_version, as_of=excluded.as_of,
             created_at=datetime('now')""",
        (result.submission_id, result.total,
         json.dumps([c.model_dump(mode="json") for c in result.components]),
         result.estimated_premium_usd, int(result.suppressed),
         result.suppressed_because, result.weights_version, result.as_of),
    )
    db.log_event(
        conn, result.submission_id, "scored",
        {"total": result.total,
         "components": {c.name: c.raw for c in result.components},
         "premium_proxy_usd": result.estimated_premium_usd,
         "suppressed": result.suppressed_because,
         "weights_version": result.weights_version, "as_of": result.as_of},
    )
    db.set_status(conn, result.submission_id, "scored")


def _context(conn: sqlite3.Connection) -> dict[str, dict]:
    """Everything the components need that does not come from the extract."""
    rows = conn.execute(
        """SELECT s.id,
                  d.outcome,
                  b.submissions_sent, b.bound,
                  COALESCE(c.is_duplicate, 0) is_duplicate,
                  EXISTS (SELECT 1 FROM documents doc
                          WHERE doc.submission_id = s.id AND doc.needs_ocr = 1) needs_ocr
           FROM submissions s
           LEFT JOIN decisions d ON d.submission_id = s.id
           LEFT JOIN brokers   b ON b.id = s.broker_id
           LEFT JOIN clearance c ON c.submission_id = s.id"""
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def run(
    conn: sqlite3.Connection,
    *,
    only: Optional[list[str]] = None,
    rebuild: bool = False,
    as_of: Optional[date] = None,
) -> dict:
    """Score every assessed submission.

    Re-running is free and expected: the weights are meant to be argued over,
    and the whole mailbox re-ranks against an edited `scoring.yaml` without a
    single model call.
    """
    when = as_of or date.today()
    if rebuild:
        conn.execute("DELETE FROM scores")

    context = _context(conn)
    ids = list(only) if only else [
        r["submission_id"] for r in conn.execute(
            "SELECT submission_id FROM decisions ORDER BY submission_id")
    ]

    scored, suppressed, stale, not_assessed = [], [], [], []
    for sid in ids:
        row = context.get(sid) or {}
        if not row.get("outcome"):
            not_assessed.append(sid)
            continue
        current = extract_stage.cached(conn, sid)
        if current is None:
            stale.append(sid)
            continue

        resolved = (naics.lookup(current.insured.naics_code or "")
                    or naics.lookup(current.insured.business_description or ""))
        result = score_one(
            sid, current,
            outcome=row["outcome"],
            hazard_grade=resolved.hazard_grade if resolved else None,
            submissions_sent=row.get("submissions_sent"),
            bound=row.get("bound"),
            needs_ocr=bool(row.get("needs_ocr")),
            is_duplicate=bool(row.get("is_duplicate")),
            as_of=when, cfg=weights(),
        )
        store(conn, result)
        (suppressed if result.suppressed else scored).append(result)

    scored.sort(key=lambda r: r.total, reverse=True)
    return {"scored": len(scored) + len(suppressed), "worklist": scored,
            "suppressed": suppressed, "stale_extracts": stale,
            "not_assessed": not_assessed, "as_of": when.isoformat(),
            "weights_version": weights().get("weights_version")}
