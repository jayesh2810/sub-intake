"""Tools available to the intake agent."""

import json
from pathlib import Path
from typing import List, Optional

from crewai.tools import tool

from intake import appetite, naics

SUBMISSIONS_DIR = Path(__file__).resolve().parents[3] / "data" / "submissions"


def _packet_dir(submission_id: str) -> Optional[Path]:
    """Resolve a submission folder, refusing anything outside the data root."""
    root = SUBMISSIONS_DIR.resolve()
    candidate = (root / submission_id).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_dir():
        return None
    return candidate


@tool("list_documents")
def list_documents(submission_id: str) -> str:
    """List the documents in a submission packet.

    Returns each filename with its size in characters. Use this first to see
    what is available before opening anything.
    """
    folder = _packet_dir(submission_id)
    if folder is None:
        available = sorted(p.name for p in SUBMISSIONS_DIR.iterdir() if p.is_dir())
        return f"No submission '{submission_id}'. Available: {', '.join(available)}"

    docs = sorted(folder.glob("*.txt"))
    if not docs:
        return f"Submission '{submission_id}' contains no readable documents."
    lines = [f"{d.name} ({len(d.read_text()):,} chars)" for d in docs]
    return f"{len(docs)} document(s) in {submission_id}:\n" + "\n".join(lines)


@tool("read_document")
def read_document(submission_id: str, filename: str) -> str:
    """Read one document from a submission packet.

    Pass a filename exactly as returned by list_documents.
    """
    folder = _packet_dir(submission_id)
    if folder is None:
        return f"No submission '{submission_id}'."

    target = (folder / filename).resolve()
    if not target.is_relative_to(folder) or not target.is_file():
        available = sorted(p.name for p in folder.glob("*.txt"))
        return f"No document '{filename}'. Available: {', '.join(available)}"
    return target.read_text()


@tool("lookup_naics")
def lookup_naics(query: str) -> str:
    """Resolve a NAICS code or business description to a class and hazard grade.

    Pass a six digit code when the documents state one. Otherwise pass the
    description of operations and the class will be matched on keywords.
    """
    hit = naics.lookup(query)
    if hit is None:
        others = naics.candidates(query)
        if not others:
            return (
                f"No class matched '{query}'. Try a shorter description, for "
                "example the primary operation only."
            )
        return "No exact match. Closest classes:\n" + json.dumps(
            [c.model_dump() for c in others], indent=2
        )
    return json.dumps(hit.model_dump(), indent=2)


@tool("check_appetite")
def check_appetite(
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
) -> str:
    """Evaluate a risk against the appetite guide and return the decision.

    This is the only source of an appetite outcome. Pass the facts exactly as
    extracted, leaving anything the documents did not state as null - do not
    substitute estimates. Where a figure is present but approximate, pass it
    and set tiv_is_estimated so the guide can weigh it accordingly.

    The returned outcome, fired rules, queue and priority are authoritative and
    must be reported unchanged.

    Dates are YYYY-MM-DD. State is a two letter code.
    """
    result = appetite.evaluate(
        naics_code=naics_code,
        business_description=business_description,
        state=state,
        lines_of_business=lines_of_business,
        total_insured_value_usd=total_insured_value_usd,
        tiv_is_estimated=tiv_is_estimated,
        annual_revenue_usd=annual_revenue_usd,
        total_incurred_5yr_usd=total_incurred_5yr_usd,
        open_claims=open_claims,
        prior_non_renewal=prior_non_renewal,
        loss_runs_provided=loss_runs_provided,
        years_in_business=years_in_business,
        effective_date=effective_date,
    )
    return json.dumps(result.model_dump(mode="json"), indent=2)


ALL_TOOLS = [list_documents, read_document, lookup_naics, check_appetite]
