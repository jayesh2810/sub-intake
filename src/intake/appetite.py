"""Appetite rules engine.

Pure Python evaluated against rules/appetite.yaml. No model involvement: the
same inputs must always produce the same outcome, and every outcome must be
traceable to a named rule.
"""

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import yaml

from intake import naics
from intake.models import AppetiteResult, FiredRule, NaicsInfo, Outcome

RULES_DIR = Path(__file__).parent / "rules"

# Worst outcome wins.
_SEVERITY = {
    Outcome.ACCEPT: 0,
    Outcome.REFER: 1,
    Outcome.PENDING_INFO: 2,
    Outcome.DECLINE: 3,
}


@lru_cache(maxsize=1)
def guide() -> dict:
    return yaml.safe_load((RULES_DIR / "appetite.yaml").read_text())


def _days_to(effective_date: Optional[str], as_of: Optional[date]) -> Optional[int]:
    if not effective_date:
        return None
    try:
        eff = datetime.strptime(effective_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (eff - (as_of or date.today())).days


def evaluate(
    *,
    naics_code: Optional[str] = None,
    business_description: Optional[str] = None,
    state: Optional[str] = None,
    lines_of_business: Optional[List[str]] = None,
    total_insured_value_usd: Optional[float] = None,
    tiv_is_estimated: Optional[bool] = None,
    annual_revenue_usd: Optional[float] = None,
    total_incurred_5yr_usd: Optional[float] = None,
    open_claims: Optional[int] = None,
    prior_non_renewal: Optional[bool] = None,
    loss_runs_provided: Optional[bool] = None,
    years_in_business: Optional[int] = None,
    effective_date: Optional[str] = None,
    as_of: Optional[date] = None,
) -> AppetiteResult:
    """Evaluate one risk against the appetite guide."""
    g = guide()
    fired: List[FiredRule] = []
    blocking: List[str] = []
    needed: List[str] = []

    resolved: Optional[NaicsInfo] = None
    if naics_code:
        resolved = naics.lookup(naics_code)
    if resolved is None and business_description:
        resolved = naics.lookup(business_description)

    # --- completeness: without these we cannot evaluate at all ---------------
    completeness = g["submission_completeness"]
    present = {
        "naics_code": resolved.code if resolved else None,
        "state": state,
        "total_insured_value_usd": total_insured_value_usd,
    }
    for field in completeness["required_for_evaluation"]:
        if present.get(field) in (None, ""):
            needed.append(field)
    if completeness.get("loss_runs_required") and not loss_runs_provided:
        needed.append("carrier loss runs")

    # An owner's estimate is not a value we can commit capacity against. The
    # capacity rules below still run on the figure - a number far above gross
    # capacity is disqualifying however soft it is - but the account cannot
    # clear completeness until confirmed values arrive.
    if completeness.get("confirmed_values_required") and tiv_is_estimated:
        needed.append("confirmed statement of values (TIV supplied is an estimate)")

    if needed:
        fired.append(
            FiredRule(
                rule_id="INCOMPLETE_SUBMISSION",
                outcome=Outcome.PENDING_INFO,
                detail=f"Cannot evaluate appetite without: {', '.join(needed)}",
            )
        )

    # --- licensing -----------------------------------------------------------
    if state and state.upper() not in g["licensed_states"]:
        fired.append(
            FiredRule(
                rule_id="STATE_NOT_LICENSED",
                outcome=Outcome.DECLINE,
                detail=f"{state.upper()} is not a licensed state on this paper",
            )
        )
        blocking.append(f"Not licensed in {state.upper()}")

    # --- class of business ---------------------------------------------------
    if resolved:
        for prohibited in g["prohibited_classes"]:
            if resolved.code == prohibited["code"]:
                fired.append(
                    FiredRule(
                        rule_id="PROHIBITED_CLASS",
                        outcome=Outcome.DECLINE,
                        detail=f"{resolved.code} {resolved.title}: {prohibited['reason']}",
                    )
                )
                blocking.append(prohibited["reason"])

        hz = g["hazard_grade"]
        if resolved.hazard_grade >= hz["decline_at_or_above"]:
            fired.append(
                FiredRule(
                    rule_id="HAZARD_GRADE_DECLINE",
                    outcome=Outcome.DECLINE,
                    detail=f"Hazard grade {resolved.hazard_grade} at or above "
                    f"decline threshold {hz['decline_at_or_above']}",
                )
            )
            blocking.append(f"Hazard grade {resolved.hazard_grade} exceeds appetite")
        elif resolved.hazard_grade >= hz["refer_at_or_above"]:
            fired.append(
                FiredRule(
                    rule_id="HAZARD_GRADE_REFER",
                    outcome=Outcome.REFER,
                    detail=f"Hazard grade {resolved.hazard_grade} requires underwriter review",
                )
            )

    # --- capacity ------------------------------------------------------------
    # Property capacity only bites if property is actually being requested. An
    # unstated line list is treated as "possibly property" rather than skipped.
    lobs = " ".join(lines_of_business or []).lower()
    property_requested = not lobs or "propert" in lobs

    if total_insured_value_usd is not None and property_requested:
        cap = g["capacity"]["property_tiv"]
        if total_insured_value_usd > cap["max_gross"]:
            fired.append(
                FiredRule(
                    rule_id="TIV_OVER_GROSS_CAPACITY",
                    outcome=Outcome.DECLINE,
                    detail=f"TIV {total_insured_value_usd:,.0f} exceeds gross capacity "
                    f"{cap['max_gross']:,.0f}",
                )
            )
            blocking.append("TIV exceeds maximum gross capacity")
        elif total_insured_value_usd > cap["max_net"]:
            fired.append(
                FiredRule(
                    rule_id="TIV_OVER_NET_CAPACITY",
                    outcome=Outcome.REFER,
                    detail=f"TIV {total_insured_value_usd:,.0f} exceeds net capacity "
                    f"{cap['max_net']:,.0f}; facultative support required",
                )
            )

    # --- loss experience -----------------------------------------------------
    le = g["loss_experience"]
    if total_incurred_5yr_usd is not None and annual_revenue_usd:
        ratio = total_incurred_5yr_usd / annual_revenue_usd
        thresholds = le["incurred_to_revenue"]
        if ratio > thresholds["decline_above"]:
            fired.append(
                FiredRule(
                    rule_id="LOSS_RATIO_DECLINE",
                    outcome=Outcome.DECLINE,
                    detail=f"5yr incurred {total_incurred_5yr_usd:,.0f} is {ratio:.1%} of "
                    f"revenue, above {thresholds['decline_above']:.1%}",
                )
            )
            blocking.append("Adverse loss experience relative to revenue")
        elif ratio > thresholds["refer_above"]:
            fired.append(
                FiredRule(
                    rule_id="LOSS_RATIO_REFER",
                    outcome=Outcome.REFER,
                    detail=f"5yr incurred is {ratio:.1%} of revenue, above "
                    f"{thresholds['refer_above']:.1%}",
                )
            )

    if open_claims is not None and open_claims > le["open_claims"]["refer_above"]:
        fired.append(
            FiredRule(
                rule_id="OPEN_CLAIMS_REFER",
                outcome=Outcome.REFER,
                detail=f"{open_claims} open claims, above {le['open_claims']['refer_above']}",
            )
        )

    if prior_non_renewal:
        fired.append(
            FiredRule(
                rule_id="PRIOR_NON_RENEWAL",
                outcome=Outcome.REFER,
                detail=le["prior_non_renewal"]["reason"],
            )
        )

    # --- operations ----------------------------------------------------------
    min_years = g["operations"]["min_years_in_business"]
    if years_in_business is not None and years_in_business < min_years:
        fired.append(
            FiredRule(
                rule_id="NEW_VENTURE",
                outcome=Outcome.REFER,
                detail=f"{years_in_business} years in business, below minimum {min_years}",
            )
        )

    # --- timing --------------------------------------------------------------
    days = _days_to(effective_date, as_of)
    max_days = completeness["max_days_to_effective"]
    if days is not None and days < max_days:
        fired.append(
            FiredRule(
                rule_id="TIGHT_EFFECTIVE_DATE",
                outcome=Outcome.REFER,
                detail=f"{days} days to effective date, inside {max_days} day threshold",
            )
        )

    outcome = max(
        (f.outcome for f in fired), key=lambda o: _SEVERITY[o], default=Outcome.ACCEPT
    )

    # A declined risk is not pending anything. The INCOMPLETE_SUBMISSION rule
    # stays on the file for audit, but we do not go back to the broker for
    # facts that cannot change the answer.
    if outcome is Outcome.DECLINE:
        needed = []

    return AppetiteResult(
        outcome=outcome,
        fired_rules=fired,
        blocking=blocking,
        information_required=needed,
        queue=_route(g, resolved, total_insured_value_usd, annual_revenue_usd),
        priority=_priority(g, days, annual_revenue_usd, bool(needed)),
        guide_version=g.get("guide_version"),
    )


def _route(
    g: dict,
    resolved: Optional[NaicsInfo],
    tiv: Optional[float],
    revenue: Optional[float],
) -> Optional[str]:
    """First matching routing rule wins."""
    for rule in g["routing"]:
        when = rule.get("when") or {}
        if "hazard_grade_min" in when:
            if not resolved or resolved.hazard_grade < when["hazard_grade_min"]:
                continue
        if "tiv_min" in when:
            if tiv is None or tiv < when["tiv_min"]:
                continue
        if "naics_prefix" in when:
            if not resolved or not resolved.code.startswith(when["naics_prefix"]):
                continue
        if "revenue_min" in when:
            if revenue is None or revenue < when["revenue_min"]:
                continue
        return rule["queue"]
    return None


def _priority(
    g: dict, days: Optional[int], revenue: Optional[float], incomplete: bool
) -> int:
    p = g["priority"]
    bands = p["days_to_effective"]
    if days is None:
        score = bands["beyond"]
    elif days <= 21:
        score = bands["within_21_days"]
    elif days <= 45:
        score = bands["within_45_days"]
    else:
        score = bands["beyond"]

    adj = p["adjustments"]
    if revenue is not None and revenue >= adj["large_account_revenue_min"]:
        score -= 1
    if incomplete:
        score += adj["incomplete_submission"]

    return max(1, min(5, score))
