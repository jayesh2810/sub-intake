"""Stage 4 - clear. Has this risk already reached us from another broker?

Two agencies shopping the same account is routine, and the carrier rule is
that the first broker to submit owns it. Quoting both is how you end up with
two prices on one risk and a broker who stops calling.

Deliberately not a model. A duplicate flag decides whose deal it is, so the
same pair of submissions has to match every single time it is evaluated - the
same answer today, next Monday, and when a broker disputes it six weeks from
now. String normalisation plus fuzzy matching gives that. An LLM's
recollection does not.

The hard part is not catching duplicates, it is not inventing them. Two
genuinely separate businesses share a name often enough that name-only
matching would start blocking legitimate submissions, which costs a great
deal more than missing a duplicate. So a match needs the name *and* the
location to agree; a name match we cannot place is raised for review rather
than acted on.
"""

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz

from intake import db
from intake.models import ClearanceResult, SubmissionExtract
from intake.pipeline import extract as extract_stage

# Tuned against the corpus, which plants three duplicate pairs written
# differently by different brokers and two businesses that genuinely share a
# name. Both directions are asserted in the tests.
NAME_MATCH = 88.0
ADDRESS_MATCH = 90.0
CITY_MATCH = 90.0

# Stripped from the end of an insured name. The legal form is not the risk:
# "Copper Basin Company" and "Copper Basin Co." are one account.
_ENTITY_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co",
    "company", "corp", "corporation", "plc", "pc", "pa",
}

_NAME_ABBREVIATIONS = {
    "mfg": "manufacturing", "inds": "industries", "intl": "international",
    "natl": "national", "bros": "brothers", "svc": "services",
    "svcs": "services", "assoc": "associates", "assocs": "associates",
    "mgmt": "management", "grp": "group", "hldgs": "holdings",
    "ent": "enterprises", "dist": "distributors",
}

_STREET_TYPES = {
    "st": "street", "str": "street", "ave": "avenue", "av": "avenue",
    "rd": "road", "dr": "drive", "ln": "lane", "ct": "court",
    "blvd": "boulevard", "pkwy": "parkway", "pky": "parkway",
    "hwy": "highway", "cir": "circle", "pl": "place", "plz": "plaza",
    "ter": "terrace", "trl": "trail", "sq": "square", "expy": "expressway",
    "wy": "way",
}

_DIRECTIONALS = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
}

_UNIT_TYPES = {
    "ste": "suite", "bldg": "building", "fl": "floor", "apt": "apartment",
    "rm": "room", "dept": "department", "unit": "unit",
}

_PUNCTUATION = re.compile(r"[^\w\s]")


def _words(value: str) -> list[str]:
    # Periods are dropped rather than split on, so "L.P." reduces to "lp" and
    # is recognised as a legal form instead of two stray letters.
    cleaned = value.casefold().replace("&", " and ").replace(".", "")
    return _PUNCTUATION.sub(" ", cleaned).split()


def normalise_name(value: Optional[str]) -> str:
    """Reduce an insured name to the part that identifies the business."""
    if not value:
        return ""
    words = [_NAME_ABBREVIATIONS.get(w, w) for w in _words(value)]
    if words[:1] == ["the"]:
        words = words[1:]

    core = list(words)
    while core and core[-1] in _ENTITY_SUFFIXES:
        core.pop()
    # A name that is nothing but legal form ("The Company") keeps what it had,
    # since an empty string would match every other empty string.
    return " ".join(core or words)


def normalise_address(value: Optional[str]) -> str:
    """Expand the abbreviations brokers type so two spellings compare equal."""
    if not value:
        return ""
    out = []
    for i, word in enumerate(_words(value)):
        if i > 0 and word in _STREET_TYPES:
            # Only past the first token: a leading "St" is Saint, not Street.
            word = _STREET_TYPES[word]
        elif word in _DIRECTIONALS:
            word = _DIRECTIONALS[word]
        elif word in _UNIT_TYPES:
            word = _UNIT_TYPES[word]
        out.append(word)
    return " ".join(out)


@dataclass(frozen=True)
class Party:
    """One submission reduced to the fields that identify the risk."""

    submission_id: str
    received_at: str
    broker: Optional[str]
    name: str
    address: str
    city: str
    state: str
    zip_code: str

    @classmethod
    def build(cls, submission_id: str, received_at: str, broker: Optional[str],
              extract: SubmissionExtract) -> "Party":
        i = extract.insured
        return cls(
            submission_id=submission_id,
            received_at=received_at or "",
            broker=broker,
            name=normalise_name(i.named_insured),
            address=normalise_address(i.mailing_address),
            city=(i.city or "").casefold().strip(),
            state=(i.state or "").upper().strip(),
            zip_code=(i.zip_code or "").strip()[:5],
        )


def compare(a: Party, b: Party) -> tuple[str, float, str]:
    """Return (verdict, score, reason) for one pair. Symmetric by construction.

    Verdict is one of `duplicate`, `review`, `distinct`. Nothing here reads
    the database or the order the pair was passed in, so the same two
    submissions always produce the same answer.
    """
    if not a.name or not b.name:
        return "distinct", 0.0, "no insured name to clear against"

    # token_sort, not token_set: token_set scores a subset as a perfect match,
    # so "Halloway Manufacturing" and "Halloway Manufacturing Services" would
    # come back 100 and two different companies would be merged.
    name_score = float(fuzz.token_sort_ratio(a.name, b.name))
    if name_score < NAME_MATCH:
        return "distinct", name_score, "different insured"

    if not (a.address and b.address and a.state and b.state):
        return ("review", name_score,
                "same insured name, location incomplete on one submission")

    same_zip = bool(a.zip_code) and a.zip_code == b.zip_code
    same_city = bool(a.city) and bool(b.city) and (
        fuzz.token_sort_ratio(a.city, b.city) >= CITY_MATCH
    )
    if not (a.state == b.state and (same_zip or same_city)):
        # Two real businesses sharing a name in different towns. Blocking
        # these is the expensive mistake.
        return "distinct", name_score, "same insured name, different location"

    # token_set here, so a suite or building number on one side does not sink
    # an otherwise identical street address.
    address_score = float(fuzz.token_set_ratio(a.address, b.address))
    score = round(0.6 * name_score + 0.4 * address_score, 1)

    if address_score >= ADDRESS_MATCH:
        return "duplicate", score, "same insured at the same address"
    return ("review", score,
            "same insured in the same city at a different address")


_VERDICT_RANK = {"distinct": 0, "review": 1, "duplicate": 2}


def find_match(party: Party, earlier: list[Party]) -> ClearanceResult:
    """Clear one submission against everything that arrived before it.

    `earlier` is ordered oldest first, and ties are resolved in its favour, so
    the account stays with the broker who submitted first.
    """
    best: Optional[tuple[tuple[int, float], Party, str, float, str]] = None
    for other in earlier:
        verdict, score, reason = compare(party, other)
        if verdict == "distinct":
            continue
        key = (_VERDICT_RANK[verdict], score)
        if best is None or key > best[0]:
            best = (key, other, verdict, score, reason)

    result = ClearanceResult(
        submission_id=party.submission_id,
        normalised_name=party.name,
        normalised_address=party.address or None,
    )
    if best is None:
        result.match_basis = "no prior submission for this risk"
        return result

    _, other, verdict, score, reason = best
    result.matched_submission_id = other.submission_id
    result.match_score = score
    result.matched_broker = other.broker
    result.is_duplicate = verdict == "duplicate"
    result.needs_review = verdict == "review"
    result.match_basis = reason
    return result


def _load_parties(conn: sqlite3.Connection) -> tuple[list[Party], list[str]]:
    """Every submission with a current extract, oldest first.

    An extract whose packet has since changed is not used - clearing a risk
    against stale facts is worse than not clearing it yet.
    """
    rows = conn.execute(
        """SELECT s.id, s.received_at, b.name broker
           FROM submissions s
           LEFT JOIN brokers b ON b.id = s.broker_id
           WHERE EXISTS (SELECT 1 FROM extracts e WHERE e.submission_id = s.id)
           ORDER BY s.received_at, s.id"""
    ).fetchall()

    parties, stale = [], []
    for row in rows:
        current = extract_stage.cached(conn, row["id"])
        if current is None:
            stale.append(row["id"])
            continue
        parties.append(
            Party.build(row["id"], row["received_at"], row["broker"], current)
        )
    return parties, stale


def store(conn: sqlite3.Connection, result: ClearanceResult) -> None:
    conn.execute(
        """INSERT INTO clearance
             (submission_id, normalised_name, normalised_address,
              matched_submission_id, match_score, is_duplicate, needs_review,
              match_basis)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(submission_id) DO UPDATE SET
             normalised_name=excluded.normalised_name,
             normalised_address=excluded.normalised_address,
             matched_submission_id=excluded.matched_submission_id,
             match_score=excluded.match_score,
             is_duplicate=excluded.is_duplicate,
             needs_review=excluded.needs_review,
             match_basis=excluded.match_basis""",
        (result.submission_id, result.normalised_name, result.normalised_address,
         result.matched_submission_id, result.match_score,
         int(result.is_duplicate), int(result.needs_review), result.match_basis),
    )

    if result.is_duplicate or result.needs_review:
        db.log_event(
            conn, result.submission_id,
            "clearance_duplicate" if result.is_duplicate else "clearance_review",
            {"matched": result.matched_submission_id,
             "first_broker": result.matched_broker,
             "score": result.match_score, "basis": result.match_basis},
        )
    db.set_status(conn, result.submission_id, "cleared")


def run(
    conn: sqlite3.Connection,
    *,
    only: Optional[list[str]] = None,
    rebuild: bool = False,
) -> dict:
    """Clear every extracted submission against the ones before it.

    Free to re-run: the verdict for a pair depends only on the two extracts,
    so the whole mailbox re-clears in well under a second with no API call.
    """
    if rebuild:
        conn.execute("DELETE FROM clearance")

    parties, stale = _load_parties(conn)
    targets = set(only) if only else None

    duplicates, review, cleared = [], [], 0
    for i, party in enumerate(parties):
        if targets is not None and party.submission_id not in targets:
            continue
        result = find_match(party, parties[:i])
        store(conn, result)
        cleared += 1
        if result.is_duplicate:
            duplicates.append(result)
        elif result.needs_review:
            review.append(result)

    missing = sorted(targets - {p.submission_id for p in parties}) if targets else []
    return {"cleared": cleared, "duplicates": duplicates, "review": review,
            "stale_extracts": stale, "not_extracted": missing}
