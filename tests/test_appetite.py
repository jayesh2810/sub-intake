"""Rules engine tests. No model calls, no network."""

from datetime import date

import pytest

from triage import naics
from triage.appetite import evaluate
from triage.models import Outcome

AS_OF = date(2026, 7, 30)


def fired(result) -> set:
    return {r.rule_id for r in result.fired_rules}


# --- class resolution -------------------------------------------------------


def test_exact_code_lookup():
    hit = naics.lookup("332119")
    assert hit is not None
    assert hit.hazard_grade == 3
    assert hit.matched_on == "code"


def test_description_lookup_when_no_code_given():
    hit = naics.lookup("Residential and light commercial re-roofs")
    assert hit is not None
    assert hit.code == "238160"
    assert hit.matched_on in {"re-roof", "roofing", "roof replacement"}


def test_longest_keyword_wins():
    hit = naics.lookup("structural metal fabrication shop")
    assert hit is not None
    assert hit.code == "332312"


def test_unmatched_description_returns_none():
    assert naics.lookup("interstellar freight brokerage") is None


# --- outcome precedence -----------------------------------------------------


def test_clean_risk_is_accepted():
    r = evaluate(
        naics_code="493110",
        state="OH",
        lines_of_business=["Property"],
        total_insured_value_usd=4_000_000,
        annual_revenue_usd=12_000_000,
        total_incurred_5yr_usd=30_000,
        loss_runs_provided=True,
        years_in_business=15,
        effective_date="2026-11-01",
        as_of=AS_OF,
    )
    assert r.outcome is Outcome.ACCEPT
    assert r.fired_rules == []


def test_decline_beats_refer():
    r = evaluate(
        naics_code="238160",           # prohibited -> decline
        state="CO",
        total_insured_value_usd=12_000_000,  # over net capacity -> refer
        annual_revenue_usd=8_900_000,
        total_incurred_5yr_usd=100_000,
        loss_runs_provided=True,
        years_in_business=20,
        effective_date="2026-12-01",
        as_of=AS_OF,
    )
    assert r.outcome is Outcome.DECLINE
    assert "PROHIBITED_CLASS" in fired(r)
    assert "TIV_OVER_NET_CAPACITY" in fired(r)


def test_missing_data_yields_pending_info():
    r = evaluate(
        naics_code="531110",
        state="NC",
        total_insured_value_usd=None,
        loss_runs_provided=False,
        as_of=AS_OF,
    )
    assert r.outcome is Outcome.PENDING_INFO
    assert "INCOMPLETE_SUBMISSION" in fired(r)
    assert "total_insured_value_usd" in r.information_required
    assert "carrier loss runs" in r.information_required


# --- individual rules -------------------------------------------------------


def test_decline_does_not_request_information():
    r = evaluate(
        naics_code="238160",              # prohibited
        state="CO",
        total_insured_value_usd=None,     # would otherwise be pending_info
        loss_runs_provided=False,
        as_of=AS_OF,
    )
    assert r.outcome is Outcome.DECLINE
    assert r.information_required == []
    # the rule stays on the file for audit even though we do not act on it
    assert "INCOMPLETE_SUBMISSION" in fired(r)


def test_unlicensed_state_declines():
    r = evaluate(
        naics_code="493110", state="CA", total_insured_value_usd=1_000_000,
        loss_runs_provided=True, as_of=AS_OF,
    )
    assert "STATE_NOT_LICENSED" in fired(r)
    assert r.outcome is Outcome.DECLINE


def test_tiv_between_net_and_gross_refers():
    r = evaluate(
        naics_code="332119", state="OH", total_insured_value_usd=14_235_000,
        annual_revenue_usd=22_400_000, total_incurred_5yr_usd=144_650,
        loss_runs_provided=True, years_in_business=28,
        effective_date="2026-09-01", as_of=AS_OF,
    )
    assert "TIV_OVER_NET_CAPACITY" in fired(r)
    assert r.outcome is Outcome.REFER


def test_tiv_above_gross_declines():
    r = evaluate(
        naics_code="332119", state="OH", total_insured_value_usd=30_000_000,
        loss_runs_provided=True, as_of=AS_OF,
    )
    assert "TIV_OVER_GROSS_CAPACITY" in fired(r)
    assert r.outcome is Outcome.DECLINE


def test_property_capacity_skipped_when_property_not_requested():
    r = evaluate(
        naics_code="332119", state="OH",
        lines_of_business=["General Liability"],
        total_insured_value_usd=14_235_000,
        loss_runs_provided=True, as_of=AS_OF,
    )
    assert "TIV_OVER_NET_CAPACITY" not in fired(r)


def test_loss_ratio_thresholds():
    common = dict(
        naics_code="493110", state="OH", total_insured_value_usd=1_000_000,
        annual_revenue_usd=10_000_000, loss_runs_provided=True, as_of=AS_OF,
    )
    assert "LOSS_RATIO_REFER" not in fired(evaluate(**common, total_incurred_5yr_usd=90_000))
    assert "LOSS_RATIO_REFER" in fired(evaluate(**common, total_incurred_5yr_usd=200_000))
    assert "LOSS_RATIO_DECLINE" in fired(evaluate(**common, total_incurred_5yr_usd=400_000))


def test_loss_ratio_skipped_without_revenue():
    r = evaluate(
        naics_code="493110", state="OH", total_insured_value_usd=1_000_000,
        annual_revenue_usd=None, total_incurred_5yr_usd=5_000_000,
        loss_runs_provided=True, as_of=AS_OF,
    )
    assert not {"LOSS_RATIO_REFER", "LOSS_RATIO_DECLINE"} & fired(r)


def test_prior_non_renewal_refers():
    r = evaluate(
        naics_code="493110", state="OH", total_insured_value_usd=1_000_000,
        loss_runs_provided=True, prior_non_renewal=True, as_of=AS_OF,
    )
    assert "PRIOR_NON_RENEWAL" in fired(r)
    assert r.outcome is Outcome.REFER


def test_tight_effective_date_refers():
    r = evaluate(
        naics_code="493110", state="OH", total_insured_value_usd=1_000_000,
        loss_runs_provided=True, effective_date="2026-08-05", as_of=AS_OF,
    )
    assert "TIGHT_EFFECTIVE_DATE" in fired(r)


def test_malformed_effective_date_is_ignored():
    r = evaluate(
        naics_code="493110", state="OH", total_insured_value_usd=1_000_000,
        loss_runs_provided=True, effective_date="next tuesday", as_of=AS_OF,
    )
    assert "TIGHT_EFFECTIVE_DATE" not in fired(r)


# --- routing and priority ---------------------------------------------------


@pytest.mark.parametrize(
    "code,tiv,revenue,expected",
    [
        ("541330", 1_000_000, 5_000_000, "specialty_casualty"),    # hazard 4
        ("332119", 14_000_000, 22_000_000, "middle_market_property"),
        ("531110", 9_000_000, 2_000_000, "habitational"),
        ("493110", 2_000_000, 15_000_000, "middle_market"),
        ("493110", 500_000, 2_000_000, "small_commercial"),
    ],
)
def test_routing(code, tiv, revenue, expected):
    r = evaluate(
        naics_code=code, state="OH", total_insured_value_usd=tiv,
        annual_revenue_usd=revenue, loss_runs_provided=True, as_of=AS_OF,
    )
    assert r.queue == expected


def test_priority_is_bounded_and_urgent_for_near_dates():
    near = evaluate(
        naics_code="493110", state="OH", total_insured_value_usd=1_000_000,
        annual_revenue_usd=25_000_000, loss_runs_provided=True,
        effective_date="2026-08-10", as_of=AS_OF,
    )
    far = evaluate(
        naics_code="493110", state="OH", total_insured_value_usd=1_000_000,
        annual_revenue_usd=1_000_000, loss_runs_provided=True,
        effective_date="2027-01-01", as_of=AS_OF,
    )
    assert near.priority == 1
    assert far.priority == 3
    assert 1 <= near.priority <= 5 and 1 <= far.priority <= 5


# --- determinism ------------------------------------------------------------


def test_same_inputs_give_identical_result():
    kwargs = dict(
        naics_code="332119", state="OH", total_insured_value_usd=14_235_000,
        annual_revenue_usd=22_400_000, total_incurred_5yr_usd=144_650,
        open_claims=1, loss_runs_provided=True, years_in_business=28,
        effective_date="2026-09-01", as_of=AS_OF,
    )
    first = evaluate(**kwargs)
    second = evaluate(**kwargs)
    assert first.model_dump() == second.model_dump()
