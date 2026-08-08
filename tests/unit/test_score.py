"""Scoring: the five components, the premium proxy, and the stage.

The claim this file has to defend is that a ranking is explainable. That
means two things and both are tested here: the total is exactly the sum of
its weighted components with nothing added on the side, and the same inputs
always produce the same number. A ranking that cannot be reproduced cannot be
argued with, and a carrier will want to argue with it.
"""

from datetime import date

import pytest

from intake import db
from intake.models import SubmissionExtract
from intake.pipeline import assess, extract, score

AS_OF = date(2026, 7, 30)
CFG = score.weights()


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "test.db")
    c.execute("""INSERT INTO brokers (id, name, agency, submissions_sent, bound)
                 VALUES ('BRK-001','Ada Ng','Ng & Co', 80, 30)""")
    yield c
    c.close()


def build(**overrides) -> SubmissionExtract:
    payload = {
        "broker": {},
        "insured": {
            "named_insured": "Copper Basin Company",
            "mailing_address": "4156 Franklin Avenue", "city": "Tucson",
            "state": "AZ", "zip_code": "61242",
            "naics_code": "423830", "annual_revenue_usd": 8_000_000.0,
            "years_in_business": 14,
        },
        "coverages_requested": [{"line_of_business": "COMMERCIAL PROPERTY"}],
        "effective_date": "2026-10-01",
        "total_insured_value_usd": 6_000_000.0,
        "tiv_is_estimated": False,
        "total_incurred_5yr_usd": 20_000.0,
        "loss_runs_provided": True,
        "extraction_confidence": 0.9,
    }
    for key, value in overrides.items():
        if key in ("insured", "broker"):
            payload[key].update(value)
        else:
            payload[key] = value
    return SubmissionExtract.model_validate(payload)


def add(conn, sid="SUB-0001", ex=None, needs_ocr=0, broker="BRK-001"):
    conn.execute(
        "INSERT INTO submissions (id, message_id, received_at, broker_id, status) "
        "VALUES (?,?, '2026-07-27T09:00:00+00:00', ?, 'parsed')",
        (sid, f"<{sid}@x>", broker))
    conn.execute(
        """INSERT INTO documents (submission_id, filename, doc_type, page_count,
                                  text, extraction_method, needs_ocr)
           VALUES (?,'acord_125.pdf','acord_125',1,?,'pdfplumber',?)""",
        (sid, f"ACORD for {sid}", needs_ocr))
    extract.store(conn, sid, ex or build(), model="fixture:test")
    return sid


# --- the premium proxy ------------------------------------------------------


def test_a_stated_expiring_premium_beats_the_rate_table():
    """The broker told us what the risk actually costs. Nothing we derive
    from a rate per $100 is going to be closer than that."""
    usd, basis = score.premium_proxy(
        total_insured_value_usd=6_000_000, annual_revenue_usd=8_000_000,
        hazard_grade=2, expiring_premium_usd=87_500, cfg=CFG)
    assert usd == 87_500
    assert "expiring" in basis


def test_the_proxy_rises_with_hazard_grade():
    grades = [score.premium_proxy(total_insured_value_usd=6_000_000,
                                  annual_revenue_usd=None, hazard_grade=g,
                                  lines_of_business=["COMMERCIAL PROPERTY"],
                                  cfg=CFG)[0]
              for g in (1, 2, 3, 4, 5)]
    assert grades == sorted(grades) and grades[0] < grades[-1]


def test_only_the_lines_being_quoted_are_priced():
    gl_only, _ = score.premium_proxy(
        total_insured_value_usd=6_000_000, annual_revenue_usd=8_000_000,
        hazard_grade=2, lines_of_business=["COMMERCIAL GENERAL LIABILITY"], cfg=CFG)
    both, _ = score.premium_proxy(
        total_insured_value_usd=6_000_000, annual_revenue_usd=8_000_000,
        hazard_grade=2, lines_of_business=["COMMERCIAL PROPERTY",
                                           "COMMERCIAL GENERAL LIABILITY"], cfg=CFG)
    assert gl_only < both


def test_an_unresolved_class_says_so_rather_than_guessing_silently():
    usd, basis = score.premium_proxy(
        total_insured_value_usd=6_000_000, annual_revenue_usd=None,
        hazard_grade=None, lines_of_business=["COMMERCIAL PROPERTY"], cfg=CFG)
    assert usd > 0 and "class unresolved" in basis


def test_no_exposure_figures_lands_mid_list_not_at_the_bottom():
    """Missing data must not read as a small account and sink out of sight."""
    usd, basis = score.premium_proxy(
        total_insured_value_usd=None, annual_revenue_usd=None,
        hazard_grade=3, cfg=CFG)
    assert usd == CFG["premium_proxy"]["unknown_exposure_usd"]
    assert "no exposure stated" in basis

    raw, _ = score.premium_value(usd, CFG)
    assert 20 < raw < 80


def test_premium_value_is_log_scaled():
    """The gap that matters is $5k to $50k, not $400k to $450k."""
    small = score.premium_value(5_000, CFG)[0]
    mid = score.premium_value(50_000, CFG)[0]
    large = score.premium_value(450_000, CFG)[0]
    top = score.premium_value(500_000, CFG)[0]
    assert (mid - small) > (top - large)


@pytest.mark.parametrize("usd,expected", [(1_000, 0.0), (2_500, 0.0),
                                          (500_000, 100.0), (5_000_000, 100.0)])
def test_premium_value_is_bounded(usd, expected):
    assert score.premium_value(usd, CFG)[0] == expected


# --- winnability -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Non-renewal issued by incumbent", "non_renewal"),
        ("Ongoing service and claims handling issues with incumbent", "service"),
        ("Incumbent restricting coverage at renewal", "coverage"),
        ("Renewal increase quoted at +27%", "price"),
        ("They want a second look", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_the_reason_for_shopping_is_classified_from_prose(text, expected):
    assert score.classify_shopping_reason(text, CFG) == expected


def test_a_non_renewal_is_not_read_as_a_rate_increase():
    """Both sentences contain 'renewal'. Only one of them is a price story."""
    assert score.classify_shopping_reason(
        "Non-renewal issued at renewal, premium was also up 20%", CFG) == "non_renewal"


def test_a_small_sample_is_pulled_toward_the_default():
    """Four binds out of twelve is not a 33% hit ratio, it is twelve tries."""
    few, _ = score.winnability(submissions_sent=12, bound=4, cfg=CFG)
    many, _ = score.winnability(submissions_sent=120, bound=40, cfg=CFG)
    default, _ = score.winnability(submissions_sent=0, bound=0, cfg=CFG)
    assert default < few < many


def test_a_broker_with_no_history_scores_the_stated_default():
    raw, basis = score.winnability(submissions_sent=0, bound=0, cfg=CFG)
    assert raw == pytest.approx(CFG["winnability"]["no_history_default"])
    assert "no history" in basis


def test_shopping_reason_and_incumbent_move_the_number():
    plain, _ = score.winnability(submissions_sent=80, bound=30, cfg=CFG)
    unhappy, _ = score.winnability(submissions_sent=80, bound=30,
                                   reason_for_shopping="poor claims service", cfg=CFG)
    thrown_off, _ = score.winnability(
        submissions_sent=80, bound=30,
        reason_for_shopping="Non-renewal issued by incumbent", cfg=CFG)
    known, _ = score.winnability(submissions_sent=80, bound=30,
                                 incumbent_known=True, cfg=CFG)
    assert thrown_off < plain < unhappy
    assert known > plain


def test_winnability_stays_inside_the_scale():
    high, _ = score.winnability(submissions_sent=100, bound=100,
                                reason_for_shopping="service", incumbent_known=True,
                                cfg=CFG)
    low, _ = score.winnability(submissions_sent=100, bound=0,
                               reason_for_shopping="non-renewal", cfg=CFG)
    assert high == 100.0 and low == 0.0


# --- urgency -----------------------------------------------------------------


@pytest.mark.parametrize("days,expected", [(3, 100), (14, 100), (20, 80), (30, 80),
                                           (45, 55), (90, 30), (200, 15)])
def test_urgency_follows_the_bands(days, expected):
    assert score.urgency(days, CFG)[0] == expected


def test_a_lapsed_effective_date_outranks_everything():
    """Not a stale submission. A missed one."""
    assert score.urgency(-2, CFG)[0] >= score.urgency(1, CFG)[0]
    assert "passed" in score.urgency(-2, CFG)[1]


def test_no_effective_date_is_neither_urgent_nor_ignored():
    raw, basis = score.urgency(None, CFG)
    assert 15 < raw < 100 and "no effective date" in basis


def test_days_are_counted_from_the_evaluation_date():
    assert score.days_to_effective("2026-08-10", date(2026, 7, 30)) == 11
    assert score.days_to_effective("bad date", AS_OF) is None
    assert score.days_to_effective(None, AS_OF) is None


# --- completeness ------------------------------------------------------------


def test_each_gap_costs_what_the_rules_say():
    full, _ = score.completeness(extraction_confidence=1.0, loss_runs_provided=True,
                                 tiv_is_estimated=False, cfg=CFG)
    no_runs, _ = score.completeness(extraction_confidence=1.0,
                                    loss_runs_provided=False,
                                    tiv_is_estimated=False, cfg=CFG)
    assert full - no_runs == CFG["completeness"]["loss_runs_missing_penalty"]


def test_an_unreadable_document_is_the_heaviest_penalty():
    c = CFG["completeness"]
    assert c["needs_ocr_penalty"] > c["loss_runs_missing_penalty"]
    scanned, basis = score.completeness(
        extraction_confidence=0.9, loss_runs_provided=True,
        tiv_is_estimated=False, needs_ocr=True, cfg=CFG)
    assert scanned == 60.0 and "unreadable" in basis


# --- the total ---------------------------------------------------------------


def test_the_total_is_exactly_its_components():
    result = score.score_one("SUB-0001", build(), outcome="accept",
                             hazard_grade=2, submissions_sent=80, bound=30,
                             as_of=AS_OF, cfg=CFG)
    assert result.total == pytest.approx(
        sum(c.contribution for c in result.components), abs=0.05)
    assert {c.name for c in result.components} == set(CFG["weights"])
    for c in result.components:
        assert c.contribution == pytest.approx(c.raw * c.weight, abs=0.01)
        assert c.basis


def test_the_same_inputs_always_give_the_same_number():
    runs = {score.score_one("SUB-0001", build(), outcome="refer", hazard_grade=2,
                            submissions_sent=80, bound=30, as_of=AS_OF,
                            cfg=CFG).total for _ in range(20)}
    assert len(runs) == 1


def test_appetite_outcome_dominates_the_ranking():
    kwargs = dict(hazard_grade=2, submissions_sent=80, bound=30, as_of=AS_OF, cfg=CFG)
    totals = [score.score_one("SUB-0001", build(), outcome=o, **kwargs).total
              for o in ("accept", "refer", "pending_info", "decline")]
    assert totals == sorted(totals, reverse=True)


# --- what stays off the worklist ---------------------------------------------


def test_a_decline_is_held_back_but_keeps_its_breakdown():
    """A declined risk that scored well on everything else is the evidence
    for whether the guide is drawn in the right place."""
    result = score.score_one("SUB-0001", build(), outcome="decline",
                             hazard_grade=2, submissions_sent=80, bound=30,
                             as_of=AS_OF, cfg=CFG)
    assert result.suppressed and result.suppressed_because == "outside appetite"
    assert result.total > 0
    assert len(result.components) == 5


def test_the_second_broker_is_held_back():
    result = score.score_one("SUB-0001", build(), outcome="accept",
                             hazard_grade=2, is_duplicate=True, as_of=AS_OF, cfg=CFG)
    assert result.suppressed
    assert "submitted first" in (result.suppressed_because or "")


def test_a_clean_submission_is_not_held_back():
    result = score.score_one("SUB-0001", build(), outcome="accept",
                             hazard_grade=2, as_of=AS_OF, cfg=CFG)
    assert not result.suppressed and result.suppressed_because is None


# --- the stage ---------------------------------------------------------------


def test_the_stage_scores_what_was_assessed(conn):
    for i in range(1, 4):
        add(conn, f"SUB-{i:04d}")
    assess.run(conn, as_of=AS_OF)
    result = score.run(conn, as_of=AS_OF)
    assert result["scored"] == 3
    assert len(result["worklist"]) == 3
    statuses = {r["status"] for r in conn.execute("SELECT status FROM submissions")}
    assert statuses == {"scored"}


def test_the_worklist_comes_back_ranked(conn):
    add(conn, "SUB-0001", build(effective_date="2027-06-01"))
    add(conn, "SUB-0002", build(effective_date="2026-08-05"))
    assess.run(conn, as_of=AS_OF)
    result = score.run(conn, as_of=AS_OF)
    totals = [r.total for r in result["worklist"]]
    assert totals == sorted(totals, reverse=True)


def test_an_unassessed_submission_is_not_scored(conn):
    add(conn)
    result = score.run(conn, only=["SUB-0001"], as_of=AS_OF)
    assert result["scored"] == 0
    assert result["not_assessed"] == ["SUB-0001"]


def test_a_stale_extract_is_not_scored(conn):
    add(conn)
    assess.run(conn, as_of=AS_OF)
    conn.execute("UPDATE documents SET text = 'a new page arrived' "
                 "WHERE submission_id = 'SUB-0001'")
    result = score.run(conn, as_of=AS_OF)
    assert result["scored"] == 0 and result["stale_extracts"] == ["SUB-0001"]


def test_an_unreadable_document_reaches_the_score(conn):
    add(conn, "SUB-0001")
    add(conn, "SUB-0002", needs_ocr=1)
    assess.run(conn, as_of=AS_OF)
    result = score.run(conn, as_of=AS_OF)
    by_id = {r.submission_id: r for r in result["worklist"]}
    readable = next(c for c in by_id["SUB-0001"].components if c.name == "completeness")
    scanned = next(c for c in by_id["SUB-0002"].components if c.name == "completeness")
    assert scanned.raw < readable.raw


def test_re_running_changes_nothing(conn):
    add(conn)
    assess.run(conn, as_of=AS_OF)
    first = score.run(conn, as_of=AS_OF)["worklist"][0].total
    second = score.run(conn, as_of=AS_OF)["worklist"][0].total
    assert first == second
    assert conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 1


def test_rebuild_discards_scores_no_longer_being_produced(conn):
    add(conn, "SUB-0001")
    add(conn, "SUB-0002")
    assess.run(conn, as_of=AS_OF)
    score.run(conn, as_of=AS_OF)
    score.run(conn, only=["SUB-0001"], rebuild=True, as_of=AS_OF)
    remaining = [r["submission_id"] for r in
                 conn.execute("SELECT submission_id FROM scores")]
    assert remaining == ["SUB-0001"]


def test_the_breakdown_is_written_to_the_audit_trail(conn):
    add(conn)
    assess.run(conn, as_of=AS_OF)
    score.run(conn, as_of=AS_OF)
    row = conn.execute(
        "SELECT detail FROM events WHERE submission_id='SUB-0001' "
        "AND event_type='scored'").fetchone()
    assert row and '"premium_proxy_usd"' in row["detail"]
    assert '"weights_version"' in row["detail"]


def test_the_stored_components_reproduce_the_stored_total(conn):
    import json
    add(conn)
    assess.run(conn, as_of=AS_OF)
    score.run(conn, as_of=AS_OF)
    row = conn.execute("SELECT total, components_json FROM scores").fetchone()
    parts = json.loads(row["components_json"])
    assert sum(p["contribution"] for p in parts) == pytest.approx(row["total"], abs=0.05)
