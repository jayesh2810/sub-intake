"""Appetite and scoring measured over the whole corpus. No API, no mailbox.

Unit tests prove each component behaves. They cannot prove the ranking is
sane, because a ranking is a property of fifty submissions at once. So this
file builds the corpus from the committed extract fixtures, runs stages 5 and
6 against it, and asserts the things a carrier would actually check: that the
order is defensible, that nothing on the worklist should have been blocked,
and that every total can be taken back apart into the numbers that produced
it.

The manifest records what the generator planted, so "ten declines" can be
checked against *which* ten rather than taken on trust.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from intake import db
from intake.models import SubmissionExtract
from intake.pipeline import assess, clearance, extract, score

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "data" / "corpus_manifest.yaml"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "extracts"
BROKERS = PROJECT_ROOT / "data" / "brokers.yaml"

pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file() or not FIXTURES.is_dir() or not BROKERS.is_file(),
    reason="corpus manifest, broker book or extract fixtures not present",
)

AS_OF = date(2026, 7, 30)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text())


@pytest.fixture(scope="module")
def extracts() -> dict[str, SubmissionExtract]:
    out = {}
    for path in sorted(FIXTURES.glob("SUB-*.json")):
        payload = json.loads(path.read_text())
        out[payload["submission_id"]] = SubmissionExtract.model_validate(
            payload["extract"])
    return out


@pytest.fixture(scope="module")
def ranked(tmp_path_factory, extracts, manifest):
    """The whole corpus through ingest-shaped rows, clearance, assess, score."""
    conn = db.init(tmp_path_factory.mktemp("corpus") / "worklist.db")

    brokers = yaml.safe_load(BROKERS.read_text())
    for b in brokers if isinstance(brokers, list) else brokers["brokers"]:
        conn.execute(
            """INSERT INTO brokers (id, name, agency, email, submissions_sent, bound)
               VALUES (?,?,?,?,?,?)""",
            (b["id"], b["name"], b["agency"], b.get("email"),
             b.get("submissions_sent", 0), b.get("bound", 0)))

    by_broker = {b["name"]: b["id"]
                 for b in (brokers if isinstance(brokers, list) else brokers["brokers"])}
    planted = {s["id"]: s for s in manifest["submissions"]}

    # Arrival in id order. The stage sorts on received_at, so this fixes which
    # of a duplicate pair is the incumbent without needing the mailbox, which
    # is not committed.
    base = datetime(2026, 7, 27, tzinfo=timezone.utc)
    for i, (sid, ex) in enumerate(sorted(extracts.items())):
        entry = planted.get(sid, {})
        conn.execute(
            "INSERT INTO submissions (id, message_id, received_at, broker_id, status) "
            "VALUES (?,?,?,?,'parsed')",
            (sid, f"<{sid}@corpus>", (base + timedelta(hours=i)).isoformat(),
             by_broker.get(entry.get("broker"))))
        # The scanned packets are the ones the generator made image-only. The
        # completeness component has to see that, so the document row carries it.
        conn.execute(
            """INSERT INTO documents (submission_id, filename, doc_type, page_count,
                                      text, extraction_method, needs_ocr)
               VALUES (?,'acord_125.pdf','acord_125',1,?,'pdfplumber',?)""",
            (sid, f"ACORD for {sid}", int(bool(entry.get("scanned")))))
        extract.store(conn, sid, ex, model="fixture:corpus")

    clearance.run(conn)
    decisions = assess.run(conn, as_of=AS_OF)
    scores = score.run(conn, as_of=AS_OF)
    yield conn, decisions, scores
    conn.close()


# --- appetite over the corpus ------------------------------------------------


def test_every_submission_gets_a_decision(ranked, manifest):
    _, decisions, _ = ranked
    assert decisions["assessed"] == manifest["count"] == 50
    assert decisions["stale_extracts"] == []
    assert sum(decisions["outcomes"].values()) == 50


def test_every_decision_names_the_rules_behind_it(ranked):
    conn, _, _ = ranked
    for row in conn.execute(
            "SELECT submission_id, outcome, fired_rules_json FROM decisions"):
        rules = json.loads(row["fired_rules_json"])
        if row["outcome"] == "accept":
            continue    # nothing fired, which is what accept means
        assert rules, f"{row['submission_id']} is {row['outcome']} with no rule"
        assert any(r["outcome"] == row["outcome"] for r in rules), (
            f"{row['submission_id']} is {row['outcome']} but no rule says so")


def test_licensing_declines_are_exactly_the_risks_sited_outside_the_licence(
        ranked, manifest, extracts):
    """The rule has to fire on every unlicensed risk and on nothing else.

    Checked against the state on the packet rather than the generator's
    flavour label, because a duplicate of an unlicensed risk is also
    unlicensed and should be caught as one.
    """
    conn, _, _ = ranked
    licensed = set(yaml.safe_load(
        (PROJECT_ROOT / "src" / "intake" / "rules" / "appetite.yaml").read_text()
    )["licensed_states"])

    flagged = {
        row["submission_id"]
        for row in conn.execute("SELECT submission_id, fired_rules_json FROM decisions")
        if any(r["rule_id"] == "STATE_NOT_LICENSED"
               for r in json.loads(row["fired_rules_json"]))
    }
    outside = {sid for sid, ex in extracts.items()
               if (assess._risk_state(ex) or "") not in licensed}

    assert flagged == outside
    planted = {s["id"] for s in manifest["submissions"]
               if s["flavour"] == "unlicensed"}
    assert planted <= flagged, "a deliberately unlicensed risk was not caught"


def test_a_decline_asks_the_broker_for_nothing(ranked):
    """There is nothing they can send that changes the answer."""
    conn, _, _ = ranked
    for row in conn.execute(
            "SELECT submission_id, information_required_json FROM decisions "
            "WHERE outcome = 'decline'"):
        assert json.loads(row["information_required_json"]) == [], row["submission_id"]


def test_every_submission_is_routed_to_a_queue(ranked):
    conn, _, _ = ranked
    unrouted = [r["submission_id"] for r in
                conn.execute("SELECT submission_id FROM decisions WHERE queue IS NULL")]
    assert unrouted == []


# --- the worklist ------------------------------------------------------------


def test_everything_is_scored(ranked, manifest):
    _, _, scores = ranked
    assert scores["scored"] == manifest["count"]
    assert scores["not_assessed"] == [] and scores["stale_extracts"] == []


def test_the_worklist_is_in_order(ranked):
    _, _, scores = ranked
    totals = [r.total for r in scores["worklist"]]
    assert totals == sorted(totals, reverse=True)


def test_every_total_decomposes_into_its_components(ranked):
    conn, _, _ = ranked
    for row in conn.execute("SELECT submission_id, total, components_json FROM scores"):
        parts = json.loads(row["components_json"])
        assert len(parts) == 5, row["submission_id"]
        assert sum(p["contribution"] for p in parts) == pytest.approx(
            row["total"], abs=0.05), row["submission_id"]
        for p in parts:
            assert p["basis"], f"{row['submission_id']} {p['name']} has no basis"


def test_nothing_outside_appetite_reaches_the_worklist(ranked):
    conn, _, scores = ranked
    declined = {r["submission_id"] for r in
                conn.execute("SELECT submission_id FROM decisions WHERE outcome='decline'")}
    assert declined, "corpus plants nothing outside appetite"
    assert not (declined & {r.submission_id for r in scores["worklist"]})


def test_the_second_broker_is_kept_off_the_worklist(ranked, manifest):
    """Clearance decided whose deal it is. The worklist has to honour that."""
    _, _, scores = ranked
    later = {max(p) for p in manifest["expected_duplicate_pairs"]}
    held = {r.submission_id for r in scores["suppressed"]}
    assert later <= held
    assert not (later & {r.submission_id for r in scores["worklist"]})


def test_the_first_broker_keeps_their_place_on_the_worklist(ranked, manifest):
    conn, _, scores = ranked
    declined = {r["submission_id"] for r in
                conn.execute("SELECT submission_id FROM decisions WHERE outcome='decline'")}
    listed = {r.submission_id for r in scores["worklist"]}
    for first, _second in ((min(p), max(p)) for p in manifest["expected_duplicate_pairs"]):
        if first not in declined:
            assert first in listed, f"{first} submitted first and vanished"


def test_a_held_back_submission_keeps_its_full_breakdown(ranked):
    """Auditable, not erased. A decline that scored well on everything else
    is the argument for revisiting the guide."""
    _, _, scores = ranked
    assert scores["suppressed"]
    for s in scores["suppressed"]:
        assert len(s.components) == 5
        assert s.suppressed_because


def test_in_appetite_risks_outrank_the_rest(ranked):
    """Not every accept above every refer - the other four components can
    legitimately reorder that. But the median has to fall the right way."""
    conn, _, scores = ranked
    outcome = {r["submission_id"]: r["outcome"] for r in
               conn.execute("SELECT submission_id, outcome FROM decisions")}
    ranks = {s.submission_id: i for i, s in enumerate(scores["worklist"])}
    accepts = sorted(ranks[s] for s in ranks if outcome[s] == "accept")
    others = sorted(ranks[s] for s in ranks if outcome[s] != "accept")
    assert accepts, "corpus has nothing in appetite"
    assert accepts[len(accepts) // 2] < others[len(others) // 2]


def test_an_unreadable_packet_scores_worse_on_completeness(ranked, manifest):
    conn, _, _ = ranked
    scanned = {s["id"] for s in manifest["submissions"] if s.get("scanned")}
    assert scanned, "corpus plants no unreadable packets"
    by_id = {}
    for row in conn.execute("SELECT submission_id, components_json FROM scores"):
        parts = {p["name"]: p["raw"] for p in json.loads(row["components_json"])}
        by_id[row["submission_id"]] = parts["completeness"]
    worst = min(by_id.values())
    assert all(by_id[s] <= worst + 35 for s in scanned)
    assert max(by_id[s] for s in scanned) < max(by_id.values())


# --- reproducibility ---------------------------------------------------------


def test_re_running_the_whole_corpus_gives_the_same_ranking(ranked):
    conn, _, first = ranked
    again = score.run(conn, as_of=AS_OF)
    assert ([r.submission_id for r in again["worklist"]]
            == [r.submission_id for r in first["worklist"]])
    assert ([r.total for r in again["worklist"]]
            == [r.total for r in first["worklist"]])


def test_the_evaluation_date_is_stored_with_every_row(ranked):
    conn, _, _ = ranked
    for table in ("decisions", "scores"):
        undated = conn.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE as_of IS NULL").fetchone()["n"]
        assert undated == 0, f"{table} rows with no evaluation date"
