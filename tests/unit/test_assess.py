"""Assess: mapping an extract onto the appetite guide, and storing the answer.

The rules themselves are covered by test_appetite.py. What is tested here is
the wiring, which is where the quiet mistakes live - reading the wrong state,
losing a fact the guide needs, or storing an outcome that cannot be
reproduced because nothing recorded what date it was evaluated against.
"""

from datetime import date

import pytest

from intake import db
from intake.models import Outcome, SubmissionExtract
from intake.pipeline import assess, extract

AS_OF = date(2026, 7, 30)


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "test.db")
    yield c
    c.close()


def build(**overrides) -> SubmissionExtract:
    """A submission that is in appetite, so a test can break one thing at a time."""
    payload = {
        "broker": {},
        "insured": {
            "named_insured": "Copper Basin Company",
            "mailing_address": "4156 Franklin Avenue", "city": "Tucson",
            "state": "AZ", "zip_code": "61242",
            # Grade 2, so a test that changes one thing sees only that thing.
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


def add(conn, sid="SUB-0001", ex=None, needs_ocr=0):
    conn.execute(
        "INSERT INTO submissions (id, message_id, received_at, status) "
        "VALUES (?,?, '2026-07-27T09:00:00+00:00', 'parsed')", (sid, f"<{sid}@x>"))
    conn.execute(
        """INSERT INTO documents (submission_id, filename, doc_type, page_count,
                                  text, extraction_method, needs_ocr)
           VALUES (?,'acord_125.pdf','acord_125',1,?,'pdfplumber',?)""",
        (sid, f"ACORD for {sid}", needs_ocr))
    extract.store(conn, sid, ex or build(), model="fixture:test")
    return sid


# --- reading the extract correctly ------------------------------------------


def test_a_clean_submission_is_accepted():
    assert assess.assess_one(build(), AS_OF).outcome is Outcome.ACCEPT


def test_licensing_follows_the_premises_not_the_mailing_address():
    """A holding company's post goes wherever the accountant is."""
    ex = build(
        insured={"state": "NY"},                       # unlicensed on this paper
        locations=[{"address": "1 Works Road", "city": "Tucson", "state": "AZ"}],
    )
    fired = [r.rule_id for r in assess.assess_one(ex, AS_OF).fired_rules]
    assert "STATE_NOT_LICENSED" not in fired

    away = build(insured={"state": "AZ"},
                 locations=[{"address": "1 Works Road", "city": "Albany",
                             "state": "NY"}])
    fired = [r.rule_id for r in assess.assess_one(away, AS_OF).fired_rules]
    assert "STATE_NOT_LICENSED" in fired


def test_mailing_address_is_used_when_there_are_no_locations():
    ex = build(insured={"state": "NY"}, locations=[])
    fired = [r.rule_id for r in assess.assess_one(ex, AS_OF).fired_rules]
    assert "STATE_NOT_LICENSED" in fired


@pytest.mark.parametrize(
    "ex,expected",
    [
        (build(open_claims=6), 6),
        (build(loss_runs=[{"policy_year": "2024-2025", "open_claims": 2},
                          {"policy_year": "2025-2026", "open_claims": 3}]), 5),
        (build(notable_claims=[{"is_open": True}, {"is_open": False},
                               {"is_open": True}]), 2),
        (build(), None),
    ],
)
def test_open_claims_are_found_however_the_packet_reported_them(ex, expected):
    assert assess._open_claims(ex) == expected


def test_silence_about_open_claims_is_not_a_count_of_zero():
    """No evidence of open claims is not evidence of none.

    Returning 0 would clear the refer threshold on a number nobody supplied.
    """
    assert assess._open_claims(build()) is None
    fired = [r.rule_id for r in assess.assess_one(build(), AS_OF).fired_rules]
    assert "OPEN_CLAIMS_REFER" not in fired


def test_a_liability_only_submission_is_not_asked_for_a_statement_of_values():
    """There is no SOV to send. Demanding one holds the file open forever."""
    gl_only = build(
        coverages_requested=[{"line_of_business": "COMMERCIAL GENERAL LIABILITY"}],
        total_insured_value_usd=None,
    )
    result = assess.assess_one(gl_only, AS_OF)
    assert "total_insured_value_usd" not in result.information_required
    assert result.outcome is Outcome.ACCEPT


def test_property_still_has_to_supply_values():
    result = assess.assess_one(build(total_insured_value_usd=None), AS_OF)
    assert "total_insured_value_usd" in result.information_required
    assert result.outcome is Outcome.PENDING_INFO


# --- the calendar ------------------------------------------------------------


def test_the_evaluation_date_changes_the_answer_and_is_recorded(conn):
    """A tight effective date is tight relative to something."""
    ex = build(effective_date="2026-08-10")
    early = assess.assess_one(ex, date(2026, 7, 1))
    late = assess.assess_one(ex, date(2026, 8, 5))
    assert "TIGHT_EFFECTIVE_DATE" not in [r.rule_id for r in early.fired_rules]
    assert "TIGHT_EFFECTIVE_DATE" in [r.rule_id for r in late.fired_rules]

    add(conn, ex=ex)
    assess.run(conn, as_of=date(2026, 8, 5))
    stored = assess.load(conn, "SUB-0001")
    assert stored is not None and stored["as_of"] == "2026-08-05"


def test_the_guide_version_travels_with_the_decision(conn):
    add(conn)
    assess.run(conn, as_of=AS_OF)
    stored = assess.load(conn, "SUB-0001")
    assert stored is not None and stored["guide_version"]


# --- the stage ---------------------------------------------------------------


def test_every_extracted_submission_is_assessed(conn):
    for i in range(1, 4):
        add(conn, f"SUB-{i:04d}")
    result = assess.run(conn, as_of=AS_OF)
    assert result["assessed"] == 3
    assert result["outcomes"] == {"accept": 3}
    statuses = {r["status"] for r in conn.execute("SELECT status FROM submissions")}
    assert statuses == {"assessed"}


def test_re_running_changes_nothing(conn):
    add(conn)
    first = assess.run(conn, as_of=AS_OF)
    second = assess.run(conn, as_of=AS_OF)
    assert first["outcomes"] == second["outcomes"]
    assert conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"] == 1


def test_editing_the_extract_changes_the_decision(conn):
    add(conn)
    assess.run(conn, as_of=AS_OF)
    assert assess.load(conn, "SUB-0001")["outcome"] == "accept"

    extract.store(conn, "SUB-0001", build(total_insured_value_usd=40_000_000.0),
                  model="fixture:test")
    assess.run(conn, as_of=AS_OF)
    assert assess.load(conn, "SUB-0001")["outcome"] == "decline"


def test_a_stale_extract_is_not_assessed(conn):
    """The packet changed after extraction. Deciding on old facts is worse
    than not deciding yet."""
    add(conn)
    conn.execute("UPDATE documents SET text = 'a new page arrived' "
                 "WHERE submission_id = 'SUB-0001'")
    result = assess.run(conn, as_of=AS_OF)
    assert result["assessed"] == 0
    assert result["stale_extracts"] == ["SUB-0001"]
    assert assess.load(conn, "SUB-0001") is None


def test_only_limits_the_run(conn):
    add(conn, "SUB-0001")
    add(conn, "SUB-0002")
    assess.run(conn, only=["SUB-0002"], as_of=AS_OF)
    assert assess.load(conn, "SUB-0001") is None
    assert assess.load(conn, "SUB-0002") is not None


def test_rebuild_discards_decisions_no_longer_being_made(conn):
    add(conn, "SUB-0001")
    add(conn, "SUB-0002")
    assess.run(conn, as_of=AS_OF)
    assess.run(conn, only=["SUB-0001"], rebuild=True, as_of=AS_OF)
    remaining = [r["submission_id"] for r in
                 conn.execute("SELECT submission_id FROM decisions")]
    assert remaining == ["SUB-0001"]


def test_the_decision_is_written_to_the_audit_trail(conn):
    add(conn)
    assess.run(conn, as_of=AS_OF)
    row = conn.execute(
        "SELECT detail FROM events WHERE submission_id='SUB-0001' "
        "AND event_type='assessed'").fetchone()
    assert row and '"outcome": "accept"' in row["detail"]
    assert '"as_of": "2026-07-30"' in row["detail"]
