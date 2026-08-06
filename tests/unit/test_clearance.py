"""Clearance: normalisation, matching, and the stage. No API calls.

Two failures matter here and they are not symmetric. Missing a duplicate
means two underwriters quote the same risk. Inventing one means a legitimate
submission gets blocked and a broker is told their account belongs to someone
else. The second is far more expensive, so the false-positive tests carry at
least as much weight as the false-negative ones.
"""

from typing import Optional

import pytest

from intake import db
from intake.models import SubmissionExtract
from intake.pipeline import clearance, extract


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "test.db")
    c.execute("INSERT INTO brokers (id, name, agency) VALUES ('BRK-001','Ada Ng','Ng & Co')")
    c.execute("INSERT INTO brokers (id, name, agency) VALUES ('BRK-002','Rey Delgado','Delgado')")
    yield c
    c.close()


def party(sid: str = "SUB-0001",
          name: Optional[str] = "Copper Basin Company",
          address: Optional[str] = "4156 Franklin Avenue",
          city: Optional[str] = "Tucson",
          state: Optional[str] = "AZ",
          zip_code: Optional[str] = "61242",
          received: str = "2026-07-27T00:00:00+00:00",
          broker: Optional[str] = "Ada Ng") -> clearance.Party:
    return clearance.Party.build(
        sid, received, broker,
        SubmissionExtract.model_validate({
            "broker": {}, "extraction_confidence": 0.9,
            "insured": {"named_insured": name, "mailing_address": address,
                        "city": city, "state": state, "zip_code": zip_code},
        }),
    )


def add_submission(conn, sid, received, broker_id="BRK-001", **insured):
    conn.execute(
        "INSERT INTO submissions (id, message_id, received_at, broker_id, status) "
        "VALUES (?,?,?,?,'parsed')", (sid, f"<{sid}@x>", received, broker_id))
    conn.execute(
        """INSERT INTO documents (submission_id, filename, doc_type, page_count,
                                  text, extraction_method, needs_ocr)
           VALUES (?,'acord_125.pdf','acord_125',1,?, 'pdfplumber', 0)""",
        (sid, f"ACORD for {sid}"))
    base = {"named_insured": "Copper Basin Company",
            "mailing_address": "4156 Franklin Avenue", "city": "Tucson",
            "state": "AZ", "zip_code": "61242"}
    base.update(insured)
    extract.store(conn, sid, SubmissionExtract.model_validate(
        {"broker": {}, "insured": base, "extraction_confidence": 0.9}),
        model="fixture:synthetic")


# --- name normalisation -----------------------------------------------------


@pytest.mark.parametrize("a,b", [
    ("Copper Basin Company", "Copper Basin Co."),
    ("Stonebridge Works Inc.", "Stonebridge Works, Incorporated"),
    ("Halloway Manufacturing Co.", "Halloway Mfg. Co"),
    ("Westmarch Partners LP", "Westmarch Partners, L.P."),
    ("Riverbend Industries LLC", "riverbend inds llc"),
    ("The Anders Group", "Anders Group"),
    ("Bell & Sons Ltd", "Bell and Sons Limited"),
])
def test_the_same_business_normalises_to_the_same_name(a, b):
    assert clearance.normalise_name(a) == clearance.normalise_name(b)


def test_entity_suffix_is_dropped_not_the_business_name():
    assert clearance.normalise_name("Copper Basin Company") == "copper basin"


def test_words_that_are_part_of_the_name_survive():
    """Group and Holdings identify the business; Inc and LLC do not."""
    assert clearance.normalise_name("Anders Group Inc.") == "anders group"
    assert clearance.normalise_name("Fenwick Holdings LLC") == "fenwick holdings"


def test_a_name_that_is_only_a_legal_form_is_not_emptied():
    """An empty normalised name would match every other empty one."""
    assert clearance.normalise_name("The Company") != ""


def test_missing_name_normalises_to_empty():
    assert clearance.normalise_name(None) == ""


# --- address normalisation --------------------------------------------------


@pytest.mark.parametrize("a,b", [
    ("4156 Franklin Avenue", "4156 Franklin Ave"),
    ("8973 Sycamore Street", "8973 Sycamore St."),
    ("6731 Industrial Parkway", "6731 Industrial Pkwy"),
    ("120 Oak Road", "120 Oak Rd"),
    ("55 N Main Street", "55 North Main St"),
    ("9 Cedar Court, Suite 4", "9 Cedar Ct Ste 4"),
])
def test_the_same_address_normalises_to_the_same_string(a, b):
    assert clearance.normalise_address(a) == clearance.normalise_address(b)


def test_a_leading_st_is_saint_not_street():
    """'St Louis Avenue' is not 'Street Louis Avenue'."""
    assert clearance.normalise_address("St Louis Avenue").startswith("st louis")


def test_different_street_numbers_stay_different():
    assert (clearance.normalise_address("120 Oak Road")
            != clearance.normalise_address("121 Oak Road"))


# --- matching ---------------------------------------------------------------


def test_same_risk_written_differently_is_a_duplicate():
    verdict, score, _ = clearance.compare(
        party(name="Copper Basin Company", address="4156 Franklin Avenue"),
        party(sid="SUB-0046", name="Copper Basin Co.", address="4156 Franklin Ave"),
    )
    assert verdict == "duplicate"
    assert score > 95


def test_the_same_name_in_another_state_is_a_different_business():
    """The expensive false positive: two real companies sharing a name."""
    verdict, _, reason = clearance.compare(
        party(name="Wraysbury Services Inc.", address="3577 Beckett Road",
              city="Green Bay", state="WI", zip_code="92168"),
        party(sid="SUB-0049", name="Wraysbury Services Inc.",
              address="270 Foundry Way", city="Lubbock", state="TX",
              zip_code="18948"),
    )
    assert verdict == "distinct"
    assert "different location" in reason


def test_a_longer_name_is_not_a_subset_match():
    """token_set_ratio would score this 100 and merge two companies."""
    verdict, _, _ = clearance.compare(
        party(name="Halloway Manufacturing Co."),
        party(sid="SUB-0002", name="Halloway Manufacturing Services Group"),
    )
    assert verdict == "distinct"


def test_unrelated_businesses_do_not_match():
    verdict, _, _ = clearance.compare(
        party(name="Copper Basin Company"),
        party(sid="SUB-0002", name="Ironwood Company"),
    )
    assert verdict == "distinct"


def test_same_name_same_city_different_address_goes_to_review():
    """Probably a second location, possibly a second business. Not our call."""
    verdict, _, _ = clearance.compare(
        party(address="4156 Franklin Avenue", zip_code="61242"),
        party(sid="SUB-0046", address="900 Ridgeway Drive", zip_code="61242"),
    )
    assert verdict == "review"


def test_a_name_match_without_a_location_is_reviewed_not_blocked():
    verdict, _, reason = clearance.compare(
        party(), party(sid="SUB-0046", address=None, state=None),
    )
    assert verdict == "review"
    assert "location incomplete" in reason


def test_a_submission_with_no_insured_name_matches_nothing():
    verdict, _, _ = clearance.compare(party(name=None), party(sid="SUB-0046", name=None))
    assert verdict == "distinct"


def test_the_verdict_does_not_depend_on_argument_order():
    a = party(name="Copper Basin Company", address="4156 Franklin Avenue")
    b = party(sid="SUB-0046", name="Copper Basin Co.", address="4156 Franklin Ave")
    assert clearance.compare(a, b) == clearance.compare(b, a)


def test_repeated_evaluation_gives_the_same_answer():
    """A duplicate flag decides whose account it is. It cannot drift."""
    a, b = party(), party(sid="SUB-0046", name="Copper Basin Co.")
    assert len({clearance.compare(a, b) for _ in range(20)}) == 1


# --- first broker owns the risk ---------------------------------------------


def test_the_earlier_submission_is_the_one_that_is_matched_against():
    first = party(sid="SUB-0002", broker="Ada Ng")
    second = party(sid="SUB-0046", name="Copper Basin Co.", broker="Rey Delgado")
    result = clearance.find_match(second, [first])
    assert result.is_duplicate
    assert result.matched_submission_id == "SUB-0002"
    assert result.matched_broker == "Ada Ng"


def test_the_first_submission_has_nothing_to_match_against():
    result = clearance.find_match(party(), [])
    assert not result.is_duplicate and not result.needs_review
    assert result.matched_submission_id is None
    assert result.match_basis


def test_a_confirmed_duplicate_outranks_a_review_flag():
    review_candidate = party(sid="SUB-0002", address=None, state=None)
    real_duplicate = party(sid="SUB-0003")
    result = clearance.find_match(
        party(sid="SUB-0046", name="Copper Basin Co."),
        [review_candidate, real_duplicate],
    )
    assert result.is_duplicate
    assert result.matched_submission_id == "SUB-0003"


# --- the stage --------------------------------------------------------------


def test_run_flags_the_later_of_a_pair(conn):
    add_submission(conn, "SUB-0002", "2026-07-28T00:00:00+00:00", "BRK-001")
    add_submission(conn, "SUB-0046", "2026-07-29T00:00:00+00:00", "BRK-002",
                   named_insured="Copper Basin Co.",
                   mailing_address="4156 Franklin Ave")
    result = clearance.run(conn)

    assert result["cleared"] == 2
    assert [r.submission_id for r in result["duplicates"]] == ["SUB-0046"]
    assert result["duplicates"][0].matched_broker == "Ada Ng"


def test_arrival_order_decides_the_owner_not_the_id(conn):
    """SUB-0046 arriving first keeps the risk, even with the higher number."""
    add_submission(conn, "SUB-0002", "2026-07-29T00:00:00+00:00", "BRK-001")
    add_submission(conn, "SUB-0046", "2026-07-28T00:00:00+00:00", "BRK-002",
                   named_insured="Copper Basin Co.")
    result = clearance.run(conn)
    assert [r.submission_id for r in result["duplicates"]] == ["SUB-0002"]


def test_run_is_idempotent(conn):
    add_submission(conn, "SUB-0002", "2026-07-28T00:00:00+00:00")
    add_submission(conn, "SUB-0046", "2026-07-29T00:00:00+00:00", "BRK-002",
                   named_insured="Copper Basin Co.")
    first = clearance.run(conn)
    second = clearance.run(conn)
    assert first["cleared"] == second["cleared"]
    assert conn.execute("SELECT COUNT(*) n FROM clearance").fetchone()["n"] == 2


def test_run_advances_status_and_records_the_match(conn):
    add_submission(conn, "SUB-0002", "2026-07-28T00:00:00+00:00")
    add_submission(conn, "SUB-0046", "2026-07-29T00:00:00+00:00", "BRK-002",
                   named_insured="Copper Basin Co.")
    clearance.run(conn)

    statuses = {r["id"]: r["status"] for r in
                conn.execute("SELECT id, status FROM submissions")}
    assert set(statuses.values()) == {"cleared"}

    row = conn.execute(
        "SELECT * FROM clearance WHERE submission_id='SUB-0046'").fetchone()
    assert row["is_duplicate"] == 1
    assert row["matched_submission_id"] == "SUB-0002"
    assert row["normalised_name"] == "copper basin"
    assert row["match_basis"]

    kinds = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM events WHERE submission_id='SUB-0046'")]
    assert "clearance_duplicate" in kinds


def test_only_clears_the_named_submission_against_all_earlier_ones(conn):
    add_submission(conn, "SUB-0002", "2026-07-28T00:00:00+00:00")
    add_submission(conn, "SUB-0046", "2026-07-29T00:00:00+00:00", "BRK-002",
                   named_insured="Copper Basin Co.")
    result = clearance.run(conn, only=["SUB-0046"])
    assert result["cleared"] == 1
    assert result["duplicates"][0].submission_id == "SUB-0046"
    assert conn.execute("SELECT COUNT(*) n FROM clearance").fetchone()["n"] == 1


def test_an_unextracted_submission_is_reported_not_silently_skipped(conn):
    conn.execute("INSERT INTO submissions (id, message_id, received_at) "
                 "VALUES ('SUB-0009','<9@x>','2026-07-28T00:00:00+00:00')")
    result = clearance.run(conn, only=["SUB-0009"])
    assert result["not_extracted"] == ["SUB-0009"]
    assert result["cleared"] == 0


def test_a_stale_extract_is_not_used_to_clear_a_risk(conn):
    """The packet changed after extraction, so the facts may no longer hold."""
    add_submission(conn, "SUB-0002", "2026-07-28T00:00:00+00:00")
    conn.execute("UPDATE documents SET text = 'a revised ACORD' "
                 "WHERE submission_id = 'SUB-0002'")
    result = clearance.run(conn)
    assert result["stale_extracts"] == ["SUB-0002"]
    assert result["cleared"] == 0


def test_a_match_that_no_longer_holds_is_withdrawn(conn):
    """A corrected extract has to be able to clear a submission, not only flag it."""
    add_submission(conn, "SUB-0002", "2026-07-28T00:00:00+00:00")
    add_submission(conn, "SUB-0046", "2026-07-29T00:00:00+00:00", "BRK-002",
                   named_insured="Copper Basin Co.")
    assert clearance.run(conn)["duplicates"]

    extract.store(conn, "SUB-0002", SubmissionExtract.model_validate({
        "broker": {}, "extraction_confidence": 0.9,
        "insured": {"named_insured": "Ironwood Company",
                    "mailing_address": "12 Elm Street", "city": "Fresno",
                    "state": "CA", "zip_code": "53209"}}))
    result = clearance.run(conn)

    assert result["duplicates"] == []
    row = conn.execute(
        "SELECT * FROM clearance WHERE submission_id='SUB-0046'").fetchone()
    assert row["is_duplicate"] == 0 and row["matched_submission_id"] is None


def test_rebuild_drops_clearance_for_a_submission_no_longer_being_cleared(conn):
    """Without --rebuild, a row for a now-stale submission would linger."""
    add_submission(conn, "SUB-0002", "2026-07-28T00:00:00+00:00")
    clearance.run(conn)
    conn.execute("UPDATE documents SET text = 'a revised ACORD' "
                 "WHERE submission_id = 'SUB-0002'")

    clearance.run(conn)
    assert conn.execute("SELECT COUNT(*) n FROM clearance").fetchone()["n"] == 1

    clearance.run(conn, rebuild=True)
    assert conn.execute("SELECT COUNT(*) n FROM clearance").fetchone()["n"] == 0
