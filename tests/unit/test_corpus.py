"""Corpus integrity.

The synthetic mailbox is test data for every stage downstream, so its defects
have to be the ones we planted. If an accidental name collision creeps in, a
clearance result stops meaning anything.

Skipped when the mailbox has not been generated:

    python scripts/generate_mailbox.py --count 50 --seed 7
"""

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAILBOX = PROJECT_ROOT / "data" / "mailbox"
MANIFEST = PROJECT_ROOT / "data" / "corpus_manifest.yaml"
BROKERS = PROJECT_ROOT / "data" / "brokers.yaml"

# The manifest is committed but the mailbox is not - it is regenerated from
# the seed. Gate on the mailbox, or a fresh clone fails instead of skipping.
pytestmark = pytest.mark.skipif(
    not MAILBOX.is_dir() or not any(MAILBOX.glob("*.eml")),
    reason="corpus not generated; run scripts/generate_mailbox.py --count 50 --seed 7",
)


@pytest.fixture(scope="module")
def manifest():
    return yaml.safe_load(MANIFEST.read_text())


@pytest.fixture(scope="module")
def by_id(manifest):
    return {s["id"]: s for s in manifest["submissions"]}


def test_every_manifest_entry_has_an_email(manifest):
    for s in manifest["submissions"]:
        assert (MAILBOX / f"{s['id']}.eml").is_file(), f"missing {s['id']}.eml"


def test_manifest_count_matches_mailbox(manifest):
    assert manifest["count"] == len(list(MAILBOX.glob("*.eml")))


def test_defect_mix_is_present(manifest):
    """A corpus of clean packets would prove nothing."""
    flavours = {}
    for s in manifest["submissions"]:
        flavours[s["flavour"]] = flavours.get(s["flavour"], 0) + 1
    for required in ("incomplete", "bad_losses", "unlicensed", "over_capacity",
                     "no_naics", "scanned", "duplicate", "decoy"):
        assert flavours.get(required, 0) > 0, f"no {required} submissions in corpus"


def test_some_submissions_state_no_naics(manifest):
    """Forces the agent to infer the class from the description of operations."""
    assert any(not s["naics_stated"] for s in manifest["submissions"])


# --- clearance ground truth -------------------------------------------------


def test_planted_duplicates_share_a_risk(manifest, by_id):
    """Same insured and same premises, written differently by two brokers."""
    assert manifest["expected_duplicate_pairs"], "no duplicates planted"
    for a, b in manifest["expected_duplicate_pairs"]:
        A, B = by_id[a], by_id[b]
        assert A["broker"] != B["broker"], f"{a}/{b} came from the same broker"
        assert A["city"] == B["city"] and A["state"] == B["state"]
        # street number identical, street type abbreviated
        assert A["address"].split()[0] == B["address"].split()[0]
        assert A["address"] != B["address"] or A["insured"] != B["insured"], (
            f"{a}/{b} are byte identical - clearance would not have to normalise"
        )


def test_decoys_share_only_the_name(manifest, by_id):
    """Different businesses that happen to be called the same thing."""
    assert manifest["expected_non_duplicate_pairs"], "no decoys planted"
    for a, b in manifest["expected_non_duplicate_pairs"]:
        A, B = by_id[a], by_id[b]
        assert A["insured"] == B["insured"], f"{a}/{b} should share a name"
        assert A["address"] != B["address"]
        assert (A["city"], A["state"]) != (B["city"], B["state"])


def test_no_unplanted_name_collisions(manifest, by_id):
    """Any collision must be one we planted, or clearance results are noise."""
    from rapidfuzz import fuzz

    planted = {
        tuple(sorted(p))
        for p in manifest["expected_duplicate_pairs"]
        + manifest["expected_non_duplicate_pairs"]
    }
    ids = list(by_id)
    stray = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if tuple(sorted((ids[i], ids[j]))) in planted:
                continue
            if fuzz.token_set_ratio(by_id[ids[i]]["insured"], by_id[ids[j]]["insured"]) > 88:
                stray.append((ids[i], ids[j]))
    assert not stray, f"unplanted name collisions: {stray}"


# --- broker book ------------------------------------------------------------


def test_broker_book_supports_winnability():
    book = yaml.safe_load(BROKERS.read_text())["brokers"]
    assert len(book) >= 8
    assert any(b["submissions_sent"] == 0 for b in book), (
        "need a broker with no history to exercise the cold-start default"
    )
    spread = [b["bound"] / b["submissions_sent"] for b in book if b["submissions_sent"]]
    assert max(spread) - min(spread) > 0.2, "hit ratios too uniform to rank on"


def test_every_submission_broker_is_in_the_book(manifest):
    names = {b["name"] for b in yaml.safe_load(BROKERS.read_text())["brokers"]}
    for s in manifest["submissions"]:
        assert s["broker"] in names, f"{s['id']} broker {s['broker']} not in book"
