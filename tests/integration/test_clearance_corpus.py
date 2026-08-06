"""Clearance measured against the whole corpus. No API, no mailbox needed.

The corpus is generated with known answers: three pairs that are the same
risk sent by two brokers, and two pairs of genuinely different businesses
that happen to share a name. `data/corpus_manifest.yaml` records both, so
"clearance found three duplicates" can be checked against *which* three
rather than taken on trust.

Every pair is compared, not just the planted ones. 50 submissions is 1,225
pairs, and the precision claim only means something if the other 1,220 come
back clean.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import json
import pytest
import yaml

from intake import db
from intake.models import SubmissionExtract
from intake.pipeline import clearance, extract

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "data" / "corpus_manifest.yaml"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "extracts"

pytestmark = pytest.mark.skipif(
    not MANIFEST.is_file() or not FIXTURES.is_dir(),
    reason="corpus manifest or extract fixtures not present",
)


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
def parties(extracts) -> dict[str, clearance.Party]:
    # Arrival is stamped in id order here. The stage sorts on received_at, so
    # this fixes which of a pair is the incumbent without depending on the
    # mailbox, which is not committed.
    base = datetime(2026, 7, 27, tzinfo=timezone.utc)
    return {
        sid: clearance.Party.build(
            sid, (base + timedelta(days=i)).isoformat(), f"Broker {i}", ex)
        for i, (sid, ex) in enumerate(sorted(extracts.items()))
    }


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


@pytest.fixture(scope="module")
def verdicts(parties) -> dict[tuple[str, str], tuple[str, float, str]]:
    ids = sorted(parties)
    return {
        _pair(a, b): clearance.compare(parties[a], parties[b])
        for i, a in enumerate(ids) for b in ids[i + 1:]
    }


def test_the_fixtures_cover_the_whole_corpus(manifest, extracts):
    assert len(extracts) == manifest["count"] == 50


def test_every_planted_duplicate_pair_is_caught(manifest, verdicts):
    missed = [p for p in manifest["expected_duplicate_pairs"]
              if verdicts[_pair(*p)][0] != "duplicate"]
    assert missed == [], f"same risk from two brokers, not caught: {missed}"


def test_businesses_that_merely_share_a_name_are_not_flagged(manifest, verdicts):
    """The expensive false positive: blocking a broker's legitimate submission."""
    wrong = [(p, verdicts[_pair(*p)]) for p in manifest["expected_non_duplicate_pairs"]
             if verdicts[_pair(*p)][0] == "duplicate"]
    assert wrong == [], f"different businesses flagged as one: {wrong}"


def test_nothing_outside_the_planted_pairs_is_called_a_duplicate(manifest, verdicts):
    expected = {_pair(*p) for p in manifest["expected_duplicate_pairs"]}
    found = {pair for pair, (verdict, _, _) in verdicts.items()
             if verdict == "duplicate"}
    assert found == expected


def test_the_planted_pairs_are_the_only_names_that_collide(manifest, parties):
    """Guards the corpus as much as the matcher - accidental name collisions
    in the test data would make the precision figure meaningless."""
    by_name: dict[str, list[str]] = {}
    for sid, p in parties.items():
        by_name.setdefault(p.name, []).append(sid)
    collisions = {n: v for n, v in by_name.items() if len(v) > 1}
    planted = {_pair(*p) for p in manifest["expected_duplicate_pairs"]}
    planted |= {_pair(*p) for p in manifest["expected_non_duplicate_pairs"]}
    assert {_pair(*v) for v in collisions.values()} == planted


# --- the stage, end to end over the corpus ----------------------------------


@pytest.fixture
def cleared(tmp_path, parties, extracts):
    conn = db.init(tmp_path / "corpus.db")
    for sid, party in parties.items():
        conn.execute(
            "INSERT INTO submissions (id, message_id, received_at, status) "
            "VALUES (?,?,?,'parsed')", (sid, f"<{sid}@corpus>", party.received_at))
        extract.store(conn, sid, extracts[sid], model="fixture:synthetic")
    result = clearance.run(conn)
    yield conn, result
    conn.close()


def test_the_stage_clears_every_submission(cleared, manifest):
    conn, result = cleared
    assert result["cleared"] == manifest["count"]
    assert result["stale_extracts"] == []
    statuses = {r["status"] for r in conn.execute("SELECT status FROM submissions")}
    assert statuses == {"cleared"}


def test_the_second_broker_is_the_one_flagged(cleared, manifest):
    """First in owns the risk, so the flag lands on the later submission."""
    _, result = cleared
    expected = {max(p) for p in manifest["expected_duplicate_pairs"]}
    assert {r.submission_id for r in result["duplicates"]} == expected


def test_each_flag_names_the_submission_it_matched(cleared, manifest):
    _, result = cleared
    owner = {max(p): min(p) for p in manifest["expected_duplicate_pairs"]}
    for r in result["duplicates"]:
        assert r.matched_submission_id == owner[r.submission_id]
        assert r.match_score and r.match_score >= clearance.NAME_MATCH
        assert r.match_basis


def test_re_running_the_whole_corpus_changes_nothing(cleared):
    conn, first = cleared
    second = clearance.run(conn)
    assert ([r.submission_id for r in second["duplicates"]]
            == [r.submission_id for r in first["duplicates"]])
    assert conn.execute("SELECT COUNT(*) n FROM clearance").fetchone()["n"] == 50
