"""Structured output schema for submission extraction.

Fields are Optional by design: submissions are routinely incomplete, and a
required field forces the model to fabricate a value. Absent facts are
reported through `missing_fields` instead.

The schema records what the documents state. It carries no underwriting
decision - appetite, pricing and routing are evaluated downstream.
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Location(BaseModel):
    """One insured premises from the SOV or application."""

    location_number: Optional[int] = Field(
        None, description="Location number as shown in the documents, e.g. 1, 2"
    )
    address: Optional[str] = Field(None, description="Full street address as written")
    city: Optional[str] = None
    state: Optional[str] = Field(None, description="Two-letter state code")
    zip_code: Optional[str] = None
    occupancy: Optional[str] = Field(
        None, description="What happens at this location, e.g. 'warehouse'"
    )
    construction_type: Optional[str] = Field(
        None, description="e.g. Joisted Masonry, Wood Frame, Non-Combustible"
    )
    year_built: Optional[int] = None
    square_feet: Optional[int] = None
    sprinklered: Optional[bool] = Field(
        None, description="True/False only if the documents state it. None if silent."
    )
    protection_class: Optional[str] = None
    building_value_usd: Optional[float] = None
    contents_value_usd: Optional[float] = None
    business_income_usd: Optional[float] = None
    location_tiv_usd: Optional[float] = Field(
        None, description="Total for this location if the documents give a subtotal"
    )


class LossRunEntry(BaseModel):
    """One row of loss history. One row per policy year per line of business."""

    policy_year: Optional[str] = Field(None, description="e.g. '2023-2024'")
    line_of_business: Optional[str] = Field(
        None, description="e.g. Property, General Liability, Auto"
    )
    claim_count: Optional[int] = None
    paid_usd: Optional[float] = None
    reserved_usd: Optional[float] = None
    incurred_usd: Optional[float] = Field(
        None, description="Total incurred = paid + reserved"
    )
    open_claims: Optional[int] = None


class NotableClaim(BaseModel):
    """A single large or open claim called out in the loss runs."""

    date_of_loss: Optional[str] = None
    line_of_business: Optional[str] = None
    incurred_usd: Optional[float] = None
    is_open: Optional[bool] = None
    description: Optional[str] = None


class CoverageRequest(BaseModel):
    """One line of business the broker is asking us to quote."""

    line_of_business: Optional[str] = None
    limit_occurrence_usd: Optional[float] = None
    limit_aggregate_usd: Optional[float] = None
    deductible_usd: Optional[float] = None
    notes: Optional[str] = Field(
        None, description="Anything qualifying the request, e.g. '2% wind deductible'"
    )


class BrokerInfo(BaseModel):
    producer_name: Optional[str] = Field(
        None,
        description="The individual broker's name as they write it, from the "
        "signature block or letterhead. Never derived from an email address: "
        "'tonya.b@agency.net' is not a name. Null if only an address is given.",
    )
    agency_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    submitted_date: Optional[str] = Field(None, description="ISO format YYYY-MM-DD")


class InsuredInfo(BaseModel):
    named_insured: Optional[str] = Field(
        None, description="Legal entity name, exactly as written in the documents"
    )
    dba: Optional[str] = None
    entity_type: Optional[str] = Field(None, description="e.g. Corporation, LLC")
    mailing_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    website: Optional[str] = None
    business_description: Optional[str] = Field(
        None, description="What the business actually does, in one or two sentences"
    )
    naics_code: Optional[str] = None
    sic_code: Optional[str] = None
    years_in_business: Optional[int] = None
    annual_revenue_usd: Optional[float] = None
    employee_count: Optional[int] = Field(
        None, description="Total headcount across all locations"
    )


class SubmissionExtract(BaseModel):
    """Everything we could pull out of one broker submission packet."""

    # --- who sent it, and about whom ---
    broker: BrokerInfo
    insured: InsuredInfo

    # --- what they want ---
    coverages_requested: List[CoverageRequest] = Field(default_factory=list)
    effective_date: Optional[str] = Field(None, description="ISO format YYYY-MM-DD")
    expiration_date: Optional[str] = Field(None, description="ISO format YYYY-MM-DD")

    # --- exposure ---
    locations: List[Location] = Field(default_factory=list)
    total_insured_value_usd: Optional[float] = Field(
        None, description="Only if stated or clearly totalled in the documents"
    )
    tiv_is_estimated: Optional[bool] = Field(
        None,
        description="True if the TIV is called approximate, unconfirmed or pending",
    )

    # --- loss history ---
    loss_runs: List[LossRunEntry] = Field(default_factory=list)
    notable_claims: List[NotableClaim] = Field(default_factory=list)
    total_incurred_5yr_usd: Optional[float] = None
    loss_runs_provided: Optional[bool] = Field(
        None,
        description="True only if actual carrier loss runs are attached. "
        "False if losses are merely described or summarised by the broker.",
    )

    # --- incumbent market ---
    incumbent_carrier: Optional[str] = None
    expiring_premium_usd: Optional[float] = None
    reason_for_shopping: Optional[str] = None
    prior_non_renewal: Optional[bool] = Field(
        None, description="True if a carrier declined to renew this risk"
    )

    # --- extraction quality ---
    missing_fields: List[str] = Field(
        default_factory=list,
        description="Plain-language names of underwriting-relevant facts that are "
        "absent from the documents, e.g. 'annual revenue', 'formal loss runs'",
    )
    ambiguities: List[str] = Field(
        default_factory=list,
        description="Facts that appear but conflict, are approximate, or are "
        "asserted by the broker without evidence",
    )
    extraction_confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="0.0 to 1.0. How complete and unambiguous the packet was. "
        "A tidy ACORD plus carrier loss runs is high. An email with round "
        "numbers and no attachments is low.",
    )


# ---------------------------------------------------------------------------
# Appetite evaluation
#
# Everything below the SubmissionExtract is produced by the rules engine in
# appetite.py, not by the model. The agent reports these values; it does not
# author them.
# ---------------------------------------------------------------------------


class Outcome(str, Enum):
    ACCEPT = "accept"
    REFER = "refer"
    DECLINE = "decline"
    PENDING_INFO = "pending_info"


class NaicsInfo(BaseModel):
    """A class code resolved against the reference table."""

    code: str
    title: str
    hazard_grade: int = Field(..., ge=1, le=5)
    notes: Optional[str] = None
    matched_on: Optional[str] = Field(
        None, description="'code' for an exact lookup, or the keyword that matched"
    )


class FiredRule(BaseModel):
    """One appetite rule that triggered, and why."""

    rule_id: str = Field(..., description="Stable identifier, e.g. PROHIBITED_CLASS")
    outcome: Outcome
    detail: str = Field(..., description="The evaluated values that triggered it")


class AppetiteResult(BaseModel):
    """Deterministic output of the appetite rules engine."""

    outcome: Outcome
    fired_rules: List[FiredRule] = Field(default_factory=list)
    blocking: List[str] = Field(
        default_factory=list, description="Reasons the risk cannot be bound as presented"
    )
    information_required: List[str] = Field(
        default_factory=list,
        description="What must be supplied before appetite can be evaluated",
    )
    queue: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=5, description="1 highest, 5 lowest")
    guide_version: Optional[str] = None


class TriageDecision(BaseModel):
    """Final triage output for one submission."""

    submission_id: str
    named_insured: Optional[str] = None

    # Copied verbatim from the appetite tool. Not the model's opinion.
    outcome: Outcome
    fired_rules: List[FiredRule] = Field(default_factory=list)
    queue: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=5)

    naics: Optional[NaicsInfo] = None

    # Authored by the model from the rule output.
    rationale: str = Field(
        ...,
        description="Two or three sentences explaining the outcome, citing the "
        "rules that fired. No new reasoning beyond what the rules established.",
    )
    information_required: List[str] = Field(
        default_factory=list,
        description="Specific items to request, phrased for the broker",
    )
    broker_message: Optional[str] = Field(
        None, description="Draft reply to the broker. Professional, brief, no pricing."
    )
    extraction_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Clearance and scoring
#
# Both deterministic. Clearance must give the same answer for the same pair of
# submissions every time, and every score must decompose into named components
# so a ranking can be defended.
# ---------------------------------------------------------------------------


class DocumentType(str, Enum):
    ACORD_125 = "acord_125"
    ACORD_126 = "acord_126"
    ACORD_140 = "acord_140"
    LOSS_RUN = "loss_run"
    SOV = "sov"
    FINANCIAL = "financial"
    SUPPLEMENTAL = "supplemental"
    EMAIL_BODY = "email_body"
    UNKNOWN = "unknown"


class BrokerRecord(BaseModel):
    """A broker and their track record with us."""

    id: str
    name: str
    agency: str
    email: Optional[str] = None
    phone: Optional[str] = None
    submissions_sent: int = 0
    bound: int = 0
    avg_bound_premium_usd: Optional[float] = None
    tier: Optional[str] = Field(None, description="e.g. preferred, standard, watch")

    @property
    def hit_ratio(self) -> Optional[float]:
        """Share of submissions that bound. None until there is any history."""
        if not self.submissions_sent:
            return None
        return self.bound / self.submissions_sent


class ClearanceResult(BaseModel):
    """Whether this risk has already reached us from somewhere else."""

    submission_id: str
    normalised_name: str
    normalised_address: Optional[str] = None
    matched_submission_id: Optional[str] = None
    match_score: Optional[float] = Field(
        None, ge=0.0, le=100.0, description="Fuzzy similarity to the matched risk"
    )
    is_duplicate: bool = False
    needs_review: bool = Field(
        False,
        description="Name matched but the location could not confirm it. "
                    "Raised for an underwriter rather than acted on.",
    )
    matched_broker: Optional[str] = Field(
        None, description="Who submitted it first. First broker owns the risk."
    )
    match_basis: Optional[str] = Field(
        None, description="Why this cleared or did not, in plain words."
    )


class ScoreComponent(BaseModel):
    """One named contribution to the triage score."""

    name: str
    raw: float = Field(..., ge=0.0, le=100.0, description="Component score before weighting")
    weight: float = Field(..., ge=0.0, le=1.0)
    contribution: float = Field(..., description="raw * weight")
    basis: str = Field(..., description="The values this was computed from")


class TriageScore(BaseModel):
    """Where a submission sits in the queue, and why."""

    submission_id: str
    total: float = Field(..., ge=0.0, le=100.0)
    components: List[ScoreComponent] = Field(default_factory=list)
    estimated_premium_usd: Optional[float] = Field(
        None,
        description="Crude indication used only to sort the queue. Not a rate, "
        "not a quote, and never to be presented as one.",
    )
    suppressed: bool = Field(
        False, description="True for declines - scored, stored, but off the worklist"
    )
    weights_version: Optional[str] = None
