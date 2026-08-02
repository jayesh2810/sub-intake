"""Generate a synthetic Monday-morning mailbox.

Produces .eml files with real PDF attachments, plus a broker book with bind
history so winnability can be scored against something.

Seeded, so the corpus is reproducible: the same seed always yields the same
50 submissions, which is what makes fixture-replay testing possible.

    python scripts/generate_mailbox.py --count 50 --seed 7

The mix is deliberate. Roughly a quarter of real submissions are missing
something material, a tenth are outside any sane appetite, and a few are the
same risk arriving from two brokers at once. A corpus of clean packets would
flatter the extractor and teach us nothing.
"""

import argparse
import random
import shutil
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import yaml
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAILBOX_DIR = PROJECT_ROOT / "data" / "mailbox"
BROKERS_FILE = PROJECT_ROOT / "data" / "brokers.yaml"
MANIFEST_FILE = PROJECT_ROOT / "data" / "corpus_manifest.yaml"

# Corpus is generated relative to this date so effective dates and urgency
# bands stay meaningful without depending on when the script is run.
AS_OF = date(2026, 7, 30)

BROKERS = [
    # name, agency, domain, sent, bound, avg premium, tier
    ("Devanshi Patel", "Harborline Insurance Brokers", "harborlinebrokers.com", 84, 31, 71000, "preferred"),
    ("Marcus Whitfield", "Keystone Risk Partners", "keystone-risk.com", 62, 19, 88000, "standard"),
    ("Tonya Bell", "Crestpoint Agency", "crestpointagency.net", 47, 6, 24000, "watch"),
    ("Jared Hollis", "Tri-State Insurance Services", "tristate-ins.com", 55, 9, 33000, "watch"),
    ("Priya Raghunathan", "Meridian Brokerage Group", "meridianbg.com", 91, 38, 104000, "preferred"),
    ("S. Okafor", "Latitude Commercial", "latitudecommercial.com", 38, 13, 57000, "standard"),
    ("Bill Trombley", "Trombley & Sons Insurance", "trombleyins.com", 29, 11, 41000, "standard"),
    ("Anna Kowalczyk", "Northgate Risk Advisors", "northgaterisk.com", 73, 27, 66000, "preferred"),
    ("Rey Delgado", "Delgado Insurance Group", "delgadoins.com", 21, 3, 19000, "watch"),
    ("Hannah Fiedler", "Copperline Brokers", "copperlinebrokers.com", 44, 16, 72000, "standard"),
    ("Wes Nakamura", "Pacific Union Brokerage", "pacunionbrokerage.com", 12, 4, 48000, "standard"),
    ("Ollie Brennan", "Brennan Risk Services", "brennanrisk.com", 0, 0, None, None),  # no history
]

# code, title, keywords for description, hazard, typical construction
CLASSES = [
    ("332119", "Metal Stamping", "Precision metal stamping and sheet metal fabrication", "Joisted Masonry"),
    ("332312", "Structural Metal Fabrication", "Fabricated structural metal for commercial construction", "Non-Combustible"),
    ("238160", "Roofing Contractor", "Residential and light commercial re-roofing", None),
    ("238220", "Plumbing / HVAC Contractor", "Commercial plumbing and HVAC installation and service", None),
    ("236116", "Multifamily Construction", "Ground-up multifamily residential construction", None),
    ("531110", "Apartments", "Garden style apartment complex, residential rental", "Wood Frame"),
    ("531120", "Commercial Real Estate", "Multi-tenant retail center, landlord operations", "Joisted Masonry"),
    ("493110", "Warehousing", "General warehousing and distribution, third party logistics", "Non-Combustible"),
    ("722511", "Restaurant", "Full service casual dining restaurant", "Joisted Masonry"),
    ("423830", "Machinery Wholesale", "Industrial machinery and equipment distribution", "Non-Combustible"),
    ("621111", "Medical Office", "Multi-physician outpatient medical practice", "Fire Resistive"),
    ("541330", "Engineering Services", "Civil and structural engineering consultancy", "Fire Resistive"),
    ("332119", "Metal Stamping", "Automotive component stamping, tier two supplier", "Joisted Masonry"),
    ("493110", "Cold Storage", "Refrigerated warehousing and distribution", "Non-Combustible"),
    ("531110", "Apartments", "Mid-rise apartment building, residential rental", "Joisted Masonry"),
]

STATES = ["OH", "IN", "MI", "PA", "NC", "GA", "TX", "AZ", "MO", "WI", "CA", "NY"]
UNLICENSED = {"CA", "NY"}  # not on the appetite guide's licensed list

CITIES = {
    "OH": ["Toledo", "Akron", "Dayton"], "IN": ["Fort Wayne", "Muncie"],
    "MI": ["Grand Rapids", "Lansing"], "PA": ["Erie", "Allentown"],
    "NC": ["Charlotte", "Greensboro"], "GA": ["Macon", "Savannah"],
    "TX": ["Waco", "Lubbock"], "AZ": ["Mesa", "Tucson"],
    "MO": ["Springfield", "Joplin"], "WI": ["Green Bay", "Racine"],
    "CA": ["Fresno", "Modesto"], "NY": ["Syracuse", "Utica"],
}

STREETS = ["Industrial Parkway", "Commerce Drive", "Beckett Road", "Old Mill Road",
           "Sierra Ridge Drive", "Warehouse Row", "Franklin Avenue", "Copper Creek Lane",
           "Sycamore Street", "Prospect Road", "Foundry Way", "Meadowbrook Court"]

NAME_PARTS_A = ["Bluewater", "Sierra Ridge", "Apex", "Ironwood", "Granite Peak", "Harbor Point",
                "Cedar Hollow", "Redstone", "Vanguard", "Summit Line", "Copper Basin",
                "Northfield", "Silver Creek", "Ridgeway", "Pinnacle", "Lakeshore",
                "Stonebridge", "Fairmount", "Brightwater", "Tallgrass", "Westmarch",
                "Quarry Hill", "Ambleside", "Thornbury", "Kestrel", "Marchwood",
                "Halloway", "Beaumont", "Draycott", "Elderfield", "Wraysbury",
                "Osgood", "Penhurst", "Calloway", "Ravensworth", "Ashcombe"]
NAME_PARTS_B = ["Manufacturing Co.", "Properties LLC", "Industries Inc.", "Contractors Inc.",
                "Holdings LLC", "Partners LP", "Group Inc.", "Enterprises LLC",
                "Works Inc.", "Company", "Fabrication Inc.", "Realty LLC",
                "Services Inc.", "Logistics LLC", "Associates LP"]

INCUMBENTS = ["Continental Mutual Insurance", "Summit Guaranty Insurance", "Great Lakes Casualty",
              "Ardent National", "Pioneer Indemnity", "Westbrook Mutual"]

SHOPPING_REASONS = [
    ("price", "Renewal increase quoted at +{pct}%"),
    ("service", "Ongoing service and claims handling issues with incumbent"),
    ("coverage", "Incumbent restricting coverage at renewal"),
    ("non_renewal", "Non-renewal issued by incumbent"),
]


# --------------------------------------------------------------------------
# PDF rendering
# --------------------------------------------------------------------------

def _pdf(path: Path, title: str, lines: list[str], image_only: bool = False) -> None:
    """Render lines to a PDF.

    image_only draws the text as vector paths with no extractable text layer,
    standing in for a scanned document. The parse stage must detect this and
    flag it rather than silently returning nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # invariant drops the creation timestamp and document id, so regenerating
    # the corpus yields byte-identical PDFs. Without it every regeneration
    # would miss the extraction cache and re-bill the whole mailbox.
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    width, height = LETTER
    y = height - 0.9 * inch

    if image_only:
        # A genuine scan: rasterise the text to a bitmap and embed the bitmap.
        # There is no text layer at all, so a text extractor returns nothing
        # and the parse stage has to notice rather than silently produce an
        # empty document.
        from PIL import Image, ImageDraw, ImageFont
        from reportlab.lib.utils import ImageReader

        px_w, px_h = 1275, 1650  # 150 dpi letter
        img = Image.new("L", (px_w, px_h), 245)
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        ty = 60
        for line in [title, ""] + lines:
            draw.text((70, ty), line[:95], fill=40, font=font)
            ty += 16
            if ty > px_h - 60:
                break
        # Light speckle so it reads as a scan rather than a clean render.
        noise = random.Random(len(lines))
        for _ in range(1200):
            x, yy = noise.randrange(px_w), noise.randrange(px_h)
            img.putpixel((x, yy), noise.randrange(120, 210))
        c.drawImage(ImageReader(img), 0, 0, width=width, height=height)
        c.save()
        return

    c.setFont("Helvetica-Bold", 12)
    c.drawString(0.9 * inch, y, title)
    y -= 0.32 * inch
    c.setFont("Courier", 8.6)
    for line in lines:
        if y < 0.75 * inch:
            c.showPage()
            c.setFont("Courier", 8.6)
            y = height - 0.9 * inch
        c.drawString(0.9 * inch, y, line[:98])
        y -= 10.5
    c.save()


def acord_lines(s: dict) -> list[str]:
    L = [
        f"DATE: {s['submitted'].strftime('%m/%d/%Y')}",
        f"AGENCY: {s['broker'][1]}",
        f"PRODUCER: {s['broker'][0]}",
        "",
        "-" * 74,
        "APPLICANT INFORMATION",
        "-" * 74,
        f"NAME (First Named Insured): {s['insured']}",
        f"MAILING ADDRESS: {s['address']}",
        f"                 {s['city']}, {s['state']} {s['zip']}",
        f"YEARS IN BUSINESS: {s['years']}",
        f"NAICS: {s['naics'] if s['show_naics'] else '(not provided)'}",
        f"DESCRIPTION OF OPERATIONS: {s['description']}",
        "",
        "-" * 74,
        "PREMISES / EXPOSURE",
        "-" * 74,
    ]
    if s["revenue"] is not None:
        L.append(f"ANNUAL REVENUE: ${s['revenue']:,}")
    else:
        L.append("ANNUAL REVENUE: ________________")
    if s["employees"] is not None:
        L.append(f"FULL TIME EMPLOYEES: {s['employees']}")
    L += [
        "",
        "-" * 74,
        "COVERAGE REQUESTED",
        "-" * 74,
    ]
    for lob in s["lobs"]:
        L.append(f"[X] {lob}")
    L += [
        f"    EACH OCCURRENCE   $1,000,000",
        f"    GENERAL AGGREGATE $2,000,000",
        f"    DEDUCTIBLE        ${s['deductible']:,}",
        "",
        f"PROPOSED EFFECTIVE DATE: {s['effective'].strftime('%m/%d/%Y')}",
        f"EXPIRATION DATE:         {(s['effective'] + timedelta(days=365)).strftime('%m/%d/%Y')}",
        "",
        "-" * 74,
        "PRIOR CARRIER",
        "-" * 74,
    ]
    if s["incumbent"]:
        L.append(f"CARRIER: {s['incumbent']}")
        if s["expiring_premium"]:
            L.append(f"ANNUAL PREMIUM: ${s['expiring_premium']:,}")
        L.append(f"REASON FOR SHOPPING: {s['shopping_text']}")
    else:
        L.append("CARRIER: (not disclosed)")
    return L


def sov_lines(s: dict) -> list[str]:
    L = [
        "STATEMENT OF VALUES",
        s["insured"],
        f"VALUES AS OF {s['submitted'].strftime('%m/%d/%Y')}",
        "",
    ]
    for i, loc in enumerate(s["locations"], start=1):
        L += [
            f"LOC {i} -- {loc['address']}, {loc['city']}, {loc['state']} {loc['zip']}",
            f"   Occupancy:      {s['class_title']}",
            f"   Construction:   {loc['construction']}",
            f"   Year Built:     {loc['year_built']}",
            f"   Square Footage: {loc['sqft']:,}",
            f"   Sprinklered:    {loc['sprinklered']}",
            "",
            f"   Building                  ${loc['building']:>12,}",
            f"   Contents / Equipment      ${loc['contents']:>12,}",
            f"   Business Income           ${loc['bi']:>12,}",
            f"   {'-' * 42}",
            f"   Location Subtotal         ${loc['building'] + loc['contents'] + loc['bi']:>12,}",
            "",
        ]
    L += ["=" * 46]
    if s["tiv_estimated"]:
        L.append(f"TOTAL INSURABLE VALUE   approx ${s['tiv']:,}   ** UNCONFIRMED **")
        L.append("Formal appraisal pending, values are owner estimates.")
    else:
        L.append(f"TOTAL INSURABLE VALUE   ${s['tiv']:,}")
    L.append("=" * 46)
    return L


def loss_run_lines(s: dict) -> list[str]:
    L = [
        f"{(s['incumbent'] or 'PRIOR CARRIER').upper()}",
        "LOSS EXPERIENCE REPORT",
        f"INSURED: {s['insured'].upper()}",
        f"VALUED AS OF {s['submitted'].strftime('%m/%d/%Y')}",
        "",
        "=" * 66,
        "POLICY YEAR   LINE   CLAIMS   PAID         INCURRED",
        "=" * 66,
    ]
    for row in s["loss_rows"]:
        L.append(
            f"{row['year']:<14}{row['lob']:<7}{row['count']:>6}   "
            f"${row['paid']:>10,}   ${row['incurred']:>10,}"
        )
    L += [
        "=" * 66,
        f"{'TOTALS':<14}{'':<7}{sum(r['count'] for r in s['loss_rows']):>6}   "
        f"{'':>11}   ${s['total_incurred']:>10,}",
        "=" * 66,
        "",
        f"OPEN CLAIMS: {s['open_claims']}",
    ]
    if s["non_renewal"]:
        L += ["", "CARRIER NOTE: Non-renewal issued. Reason code 04 -",
              "              adverse loss experience."]
    return L


# --------------------------------------------------------------------------
# Submission synthesis
# --------------------------------------------------------------------------

def make_submission(
    rng: random.Random, idx: int, flavour: str, used_names: set[str] | None = None
) -> dict:
    """Build one submission's facts. flavour drives the deliberate defects.

    Names are kept unique so that any name collision in the corpus is one we
    planted, not one the generator stumbled into. Clearance test data has to be
    controlled in both directions or the results mean nothing.
    """
    broker = rng.choice(BROKERS)
    naics, class_title, description, construction = rng.choice(CLASSES)
    state = rng.choice(STATES) if flavour != "unlicensed" else rng.choice(sorted(UNLICENSED))
    city = rng.choice(CITIES[state])

    used = used_names if used_names is not None else set()
    for _ in range(400):
        insured = f"{rng.choice(NAME_PARTS_A)} {rng.choice(NAME_PARTS_B)}"
        if insured not in used:
            break
    else:
        insured = f"{rng.choice(NAME_PARTS_A)} {rng.choice(NAME_PARTS_B)} {idx}"
    used.add(insured)

    revenue = rng.choice([1_800_000, 4_200_000, 8_900_000, 14_500_000, 22_400_000,
                          31_000_000, 48_000_000, 76_000_000])
    employees = max(4, int(revenue / rng.uniform(120_000, 320_000)))
    years = rng.choice([2, 3, 6, 11, 18, 28, 35])

    n_locs = rng.choice([1, 1, 1, 2, 2, 3])
    locations = []
    for _ in range(n_locs):
        building = rng.randrange(400_000, 9_000_000, 50_000)
        locations.append({
            "address": f"{rng.randrange(100, 9900)} {rng.choice(STREETS)}",
            "city": city, "state": state, "zip": f"{rng.randrange(10000, 99999)}",
            "construction": construction or "Joisted Masonry",
            "year_built": rng.randrange(1968, 2019),
            "sqft": rng.randrange(6_000, 140_000, 1000),
            "sprinklered": rng.choice(["Yes", "No"]),
            "building": building,
            "contents": rng.randrange(150_000, 5_000_000, 25_000),
            "bi": rng.randrange(0, 2_500_000, 50_000),
        })
    tiv = sum(l["building"] + l["contents"] + l["bi"] for l in locations)
    if flavour == "over_capacity":
        scale = rng.uniform(3.0, 5.0)
        for l in locations:
            l["building"] = int(l["building"] * scale)
        tiv = sum(l["building"] + l["contents"] + l["bi"] for l in locations)

    reason_key, reason_tpl = rng.choice(SHOPPING_REASONS)
    if flavour == "bad_losses":
        reason_key, reason_tpl = SHOPPING_REASONS[3]
    non_renewal = reason_key == "non_renewal"

    # Loss history sized against revenue so the ratio rules have something
    # real to bite on.
    severity = {"bad_losses": rng.uniform(0.045, 0.11)}.get(flavour, rng.uniform(0.0005, 0.012))
    total_incurred = int(revenue * severity)
    years_of_loss = ["2021-2022", "2022-2023", "2023-2024", "2024-2025", "2025-2026"]
    weights = [rng.random() for _ in years_of_loss]
    wsum = sum(weights) or 1.0
    loss_rows, running = [], 0
    for i, y in enumerate(years_of_loss):
        amt = int(total_incurred * weights[i] / wsum)
        running += amt
        loss_rows.append({
            "year": y, "lob": rng.choice(["PROP", "GL"]),
            "count": 0 if amt == 0 else rng.randint(1, 6),
            "paid": int(amt * rng.uniform(0.6, 1.0)), "incurred": amt,
        })
    total_incurred = running

    effective = AS_OF + timedelta(days=rng.choice([9, 12, 18, 25, 33, 45, 62, 78, 95, 130]))
    submitted = AS_OF - timedelta(days=rng.randint(0, 3))

    lobs = rng.choice([
        ["COMMERCIAL PROPERTY", "COMMERCIAL GENERAL LIABILITY"],
        ["COMMERCIAL GENERAL LIABILITY"],
        ["COMMERCIAL PROPERTY"],
    ])

    return {
        "idx": idx, "flavour": flavour, "broker": broker,
        "naics": naics, "class_title": class_title, "description": description,
        "show_naics": flavour != "no_naics",
        "insured": insured, "address": locations[0]["address"], "city": city,
        "state": state, "zip": locations[0]["zip"],
        "revenue": None if flavour == "incomplete" else revenue,
        "employees": employees, "years": years,
        "locations": locations, "tiv": tiv,
        "tiv_estimated": flavour == "incomplete",
        "lobs": lobs, "deductible": rng.choice([5_000, 10_000, 25_000, 50_000]),
        "effective": effective, "submitted": submitted,
        "incumbent": None if flavour == "incomplete" else rng.choice(INCUMBENTS),
        "expiring_premium": None if flavour == "incomplete" else rng.randrange(18_000, 240_000, 500),
        "shopping_reason": reason_key,
        "shopping_text": reason_tpl.format(pct=rng.choice([12, 18, 22, 27, 34])),
        "loss_rows": loss_rows, "total_incurred": total_incurred,
        "open_claims": rng.randint(0, 5), "non_renewal": non_renewal,
        "has_loss_runs": flavour != "incomplete",
        "scanned": flavour == "scanned",
    }


def email_body(s: dict) -> str:
    b = s["broker"]
    lines = [f"Hi team,", ""]
    if s["flavour"] == "incomplete":
        lines += [
            f"Can you look at {s['insured']}? {s['description']}, out of {s['city']}.",
            "",
            f"TIV is somewhere around ${s['tiv']:,} but I'm waiting on an updated",
            "appraisal from the client, will forward when I have it.",
            "",
            "I don't have loss runs yet - client mentions a claim a couple of years",
            "back but couldn't recall the amount. Chasing them now.",
            "",
            f"Effective {s['effective'].strftime('%m/%d/%Y')}, they have some flexibility.",
        ]
    elif s["flavour"] == "bad_losses":
        lines += [
            f"I know this class is tough but hear me out. {s['insured']},",
            f"{s['description']}, {s['city']} {s['state']}.",
            "",
            f"Revenue about ${s['revenue']:,}, {s['employees']} employees.",
            "",
            "They've had a rough few years but ownership changed and the new owner",
            "brought in a real safety program. Please look past the older numbers,",
            "that was the prior owner.",
            "",
            f"Need terms by {s['effective'].strftime('%m/%d/%Y')}, current policy is non-renewing.",
        ]
    else:
        lines += [
            f"Please find attached a new submission for {s['insured']}.",
            f"{s['description']}, based in {s['city']}, {s['state']}.",
            "",
            f"TIV is about ${s['tiv']:,} across {len(s['locations'])} location(s).",
        ]
        if s["revenue"]:
            lines.append(f"Revenue roughly ${s['revenue']:,} last FY.")
        lines += [
            "",
            f"Looking for {' and '.join(x.title() for x in s['lobs'])}.",
            f"Effective {s['effective'].strftime('%m/%d/%Y')}.",
        ]
        if s["incumbent"]:
            lines.append(f"Incumbent is {s['incumbent']}. {s['shopping_text']}.")
    if s["scanned"]:
        lines += ["", "Apologies, the loss runs came to us as a scan."]
    lines += ["", "Thanks,", b[0], b[1]]
    return "\n".join(lines)


def build_eml(s: dict, out_dir: Path) -> Path:
    b = s["broker"]
    sender = f"{b[0].split()[0].lower()}.{b[0].split()[-1].lower()}@{b[2]}"
    msg = EmailMessage()
    msg["Message-ID"] = f"<sub-{s['idx']:04d}@{b[2]}>"
    msg["From"] = f"{b[0]} <{sender}>"
    msg["To"] = "newsubmissions@meridiancasualty.com"
    msg["Date"] = datetime.combine(s["submitted"], datetime.min.time()).strftime(
        "%a, %d %b %Y %H:%M:%S -0500"
    )
    lob_short = "/".join("GL" if "LIABILITY" in x else "Prop" for x in s["lobs"])
    msg["Subject"] = (
        f"New Submission - {s['insured']} - {lob_short} - "
        f"Eff {s['effective'].strftime('%m/%d/%y')}"
    )
    msg.set_content(email_body(s))

    tmp = out_dir / f".tmp-{s['idx']:04d}"
    tmp.mkdir(parents=True, exist_ok=True)
    attachments = []

    p = tmp / "acord_125.pdf"
    _pdf(p, "ACORD 125 - COMMERCIAL INSURANCE APPLICATION", acord_lines(s))
    attachments.append(p)

    if "COMMERCIAL PROPERTY" in s["lobs"]:
        p = tmp / "statement_of_values.pdf"
        _pdf(p, "STATEMENT OF VALUES", sov_lines(s))
        attachments.append(p)

    if s["has_loss_runs"]:
        p = tmp / "loss_runs.pdf"
        _pdf(p, "LOSS EXPERIENCE REPORT", loss_run_lines(s), image_only=s["scanned"])
        attachments.append(p)

    for path in attachments:
        msg.add_attachment(
            path.read_bytes(), maintype="application", subtype="pdf", filename=path.name
        )

    # EmailMessage picks a random MIME boundary per message, which would make
    # every regeneration byte-different for no reason. Pin it to the index.
    msg.set_boundary(f"==============={s['idx']:016d}==")

    eml = out_dir / f"SUB-{s['idx']:04d}.eml"
    eml.write_bytes(msg.as_bytes())
    shutil.rmtree(tmp)
    return eml


def flavours_for(count: int, rng: random.Random) -> list[str]:
    """The deliberate defect mix. Duplicates are handled separately."""
    n_incomplete = round(count * 0.22)
    n_bad = round(count * 0.06)
    n_unlicensed = round(count * 0.04)
    n_over = round(count * 0.06)
    n_no_naics = round(count * 0.10)
    n_scanned = 3
    plan = (["incomplete"] * n_incomplete + ["bad_losses"] * n_bad +
            ["unlicensed"] * n_unlicensed + ["over_capacity"] * n_over +
            ["no_naics"] * n_no_naics + ["scanned"] * n_scanned)
    plan += ["clean"] * max(0, count - len(plan))
    plan = plan[:count]
    rng.shuffle(plan)
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a synthetic submission mailbox")
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--duplicates", type=int, default=3,
                    help="pairs of the same risk from different brokers")
    ap.add_argument("--decoys", type=int, default=2,
                    help="same insured name, genuinely different risk - clearance "
                         "must NOT flag these")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    if MAILBOX_DIR.exists():
        shutil.rmtree(MAILBOX_DIR)
    MAILBOX_DIR.mkdir(parents=True)

    planted = args.duplicates + args.decoys
    used_names: set[str] = set()
    subs = [make_submission(rng, i + 1, f, used_names)
            for i, f in enumerate(flavours_for(args.count - planted, rng))]

    # Duplicates: same risk, different broker, written differently. The name
    # and address are deliberately abbreviated so clearance has to normalise
    # rather than string-compare.
    ABBREV = [("Manufacturing", "Mfg."), ("Company", "Co."), ("Industries", "Inds."),
              ("Parkway", "Pkwy"), ("Drive", "Dr"), ("Road", "Rd"), ("Avenue", "Ave"),
              ("Street", "St"), ("Court", "Ct"), ("Lane", "Ln")]
    next_idx = args.count - planted + 1
    originals = rng.sample(subs, args.duplicates)
    for orig in originals:
        dup = dict(orig)
        dup["idx"] = next_idx; next_idx += 1
        other = [b for b in BROKERS if b != orig["broker"]]
        dup["broker"] = rng.choice(other)
        name, addr = dup["insured"], dup["address"]
        for long, short in ABBREV:
            name = name.replace(long, short)
            addr = addr.replace(long, short)
        dup["insured"], dup["address"] = name, addr
        dup["flavour"] = "duplicate"
        dup["tiv"] = int(round(dup["tiv"], -5))  # broker rounds it
        dup["submitted"] = orig["submitted"] + timedelta(days=1)
        subs.append(dup)

    # Decoys: a real business genuinely shares a name with another. Same
    # insured name, different city, state and address. Clearance must reject
    # these, or it will start blocking legitimate submissions - a far more
    # expensive failure than missing a duplicate.
    decoy_sources = rng.sample([s for s in subs if s["flavour"] != "duplicate"], args.decoys)
    decoy_pairs = []
    for src in decoy_sources:
        decoy = make_submission(rng, next_idx, "clean", used_names)
        next_idx += 1
        decoy["insured"] = src["insured"]      # same name
        decoy["flavour"] = "decoy"             # everything else differs
        decoy["broker"] = rng.choice([b for b in BROKERS if b != src["broker"]])
        subs.append(decoy)
        decoy_pairs.append((src, decoy))

    for s in subs:
        build_eml(s, MAILBOX_DIR)

    brokers = [
        {"id": f"BRK-{i+1:03d}", "name": n, "agency": a,
         "email": f"{n.split()[0].lower()}.{n.split()[-1].lower()}@{d}",
         "submissions_sent": sent, "bound": bound,
         "avg_bound_premium_usd": avg, "tier": tier}
        for i, (n, a, d, sent, bound, avg, tier) in enumerate(BROKERS)
    ]
    BROKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    BROKERS_FILE.write_text(yaml.safe_dump(
        {"as_of": AS_OF.isoformat(), "brokers": brokers}, sort_keys=False))

    # Ground truth for the tests. Without this, "clearance found 3 duplicates"
    # is unverifiable - we would not know whether they were the right three.
    manifest = {
        "seed": args.seed,
        "as_of": AS_OF.isoformat(),
        "count": len(subs),
        "submissions": [
            {"id": f"SUB-{s['idx']:04d}", "flavour": s["flavour"],
             "insured": s["insured"], "address": s["address"],
             "city": s["city"], "state": s["state"],
             "broker": s["broker"][0], "naics": s["naics"],
             "naics_stated": s["show_naics"], "tiv": s["tiv"],
             "has_loss_runs": s["has_loss_runs"], "scanned": s["scanned"]}
            for s in sorted(subs, key=lambda x: x["idx"])
        ],
        # Clearance must flag every pair here...
        "expected_duplicate_pairs": [
            [f"SUB-{o['idx']:04d}", f"SUB-{d['idx']:04d}"]
            for o, d in zip(originals, [s for s in subs if s["flavour"] == "duplicate"])
        ],
        # ...and must flag none of these, despite the identical insured name.
        "expected_non_duplicate_pairs": [
            [f"SUB-{a['idx']:04d}", f"SUB-{b['idx']:04d}"] for a, b in decoy_pairs
        ],
    }
    MANIFEST_FILE.write_text(yaml.safe_dump(manifest, sort_keys=False))

    counts: dict[str, int] = {}
    for s in subs:
        counts[s["flavour"]] = counts.get(s["flavour"], 0) + 1
    print(f"Generated {len(subs)} submissions in {MAILBOX_DIR.relative_to(PROJECT_ROOT)}")
    for k in sorted(counts):
        print(f"  {k:<14} {counts[k]}")
    print(f"Broker book: {BROKERS_FILE.relative_to(PROJECT_ROOT)} ({len(brokers)} brokers)")
    print(f"Manifest:    {MANIFEST_FILE.relative_to(PROJECT_ROOT)}")
    print(f"  duplicate pairs to catch:  {len(manifest['expected_duplicate_pairs'])}")
    print(f"  name-collision decoys:     {len(manifest['expected_non_duplicate_pairs'])}")


if __name__ == "__main__":
    main()
