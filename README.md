# Submission Triage

A small commercial carrier receives a hundred or more broker submissions on a
Monday morning — email with ACORD applications, statements of values, loss runs
and financials attached as PDFs. Large carriers have portals and API feeds.
Small ones have one underwriter opening every message by hand and deciding what
to work first by experience and gut feel.

The failure mode is not bad decisions. It is submissions that never get opened.
The bottom of the inbox gets a late response or none, the broker stops sending,
and the premium is gone. Quote turnaround drives broker behaviour, which drives
hit ratio.

So the goal is to **acknowledge and rank every submission within the day**, not
to underwrite better than the underwriter. The output is a ranked worklist that
can be worked top-down, where every position is explainable.

Built on CrewAI with Google Gemini. Runs on a synthetic corpus.

## Pipeline

```
data/mailbox/*.eml
  1 ingest    register submissions, write packets to disk    deterministic
  2 parse     PDF text, classify each document               deterministic
  3 extract   read the packet into structured facts          AGENT
  4 clear     has this risk already reached us?              deterministic
  5 assess    appetite outcome and the rules that fired      deterministic
  6 score     triage score and its components                deterministic
  7 route     queue, priority, drafted broker reply          deterministic + LLM
```

Stages 1–6 are built. Each stage is idempotent, independently runnable and
persisted, so any one can be re-run without undoing the others.

**Stage 3 is the only stage that calls a model.** Everything downstream is a
pure function over what it produced, which is what makes the ranking logic free
to iterate on and testable without an API key.

Status lifecycle, persisted per submission:

```
received → parsed → extracted → cleared → assessed → scored → queued
         → assigned | declined | pending_info | closed
```

## Where the agent is

Stage 3. The agent sees the document list, decides what to open, reads pages,
and returns structured facts. That is genuine branching: a packet with a NAICS
code on the ACORD takes a different path from one where the class has to be
inferred from a description of operations.

It has two tools:

| Tool | Purpose |
|---|---|
| `list_documents` | See what arrived in the packet |
| `read_document` | Open one document at a time |

It deliberately has no others. Appetite, clearance, class resolution and
scoring are deterministic stages the pipeline runs itself — putting them behind
tools would let a model paraphrase a decision instead of reporting it.

**The model does language; Python does decisions.** Reading messy documents and
writing to a broker are language problems. Whether a risk is in appetite, who
owns it, and where it sits in the queue are not.

## Clearance

Two agencies shopping the same account is routine, and the carrier rule is that
the first broker to submit owns it. Quoting both is how you end up with two
prices on one risk.

Clearance normalises the insured name and address — legal forms dropped, street
abbreviations expanded — then fuzzy matches against everything received
earlier. It is not a model: a duplicate flag decides whose deal it is, so the
same pair has to produce the same answer today, next Monday, and when a broker
disputes it six weeks from now.

The hard part is not catching duplicates, it is not inventing them. Two
separate businesses share a name often enough that name-only matching would
start blocking legitimate submissions, which costs far more than a missed
duplicate. So a match needs the name *and* the location to agree:

| Verdict | Condition |
|---|---|
| `duplicate` | Same insured, same city, same address |
| `review` | Same insured, but the location cannot confirm it |
| `distinct` | Different insured, or the same name in a different place |

Only `duplicate` blocks. A name match that cannot be placed goes to an
underwriter rather than being acted on.

Measured on the corpus, which plants three duplicate pairs and two businesses
that genuinely share a name: all 1,225 pairs are compared, all three duplicates
are caught, and no other pair is flagged.

## Appetite

The rules engine in `appetite.py` runs `rules/appetite.yaml` and returns an
outcome with every rule that fired.

| Outcome | Meaning |
|---|---|
| `accept` | In appetite, proceed to quote |
| `refer` | Requires underwriter review before terms |
| `decline` | Outside appetite as presented |
| `pending_info` | Cannot be evaluated until the broker supplies more |

Precedence when several fire: `decline` > `pending_info` > `refer` > `accept`.
A declined risk returns no information request — the file records the gap for
audit, but there is nothing to ask for.

What is required to evaluate depends on what is being quoted. A liability-only
submission has no statement of values, so asking it for a TIV would hold the
file open forever waiting on a document that was never going to exist.

Licensing follows the premises rather than the mailing address. A holding
company's post goes wherever the accountant is.

Both this stage and the score read the calendar — a tight effective date is
tight relative to something — so the date evaluated against is stored on every
row and `--as-of` pins it.

## Scoring

Five components, each 0–100, combined as a weighted sum. Weights live in
`rules/scoring.yaml`.

| Component | Weight | Basis |
|---|---|---|
| Appetite fit | 0.35 | accept 100, refer 60, pending_info 40, decline 0 |
| Premium value | 0.25 | log-scaled premium indication |
| Winnability | 0.20 | broker hit ratio, adjusted for why they are shopping |
| Urgency | 0.15 | days to effective date |
| Completeness | 0.05 | extraction confidence, less penalties for what is missing |

The total is exactly the sum of the weighted components — a reader who
multiplies the printed numbers gets the printed answer, and a test fails if
that stops being true. Every component carries the values it was computed
from:

```
  score      78.1
    premium proxy  ~$70,000   (triage indication only, not a rate)
    appetite_fit    100.0 x 0.35 = 35.00   appetite outcome accept
    premium_value    62.9 x 0.25 = 15.72   $70,000 indicated - expiring premium as stated
    winnability      73.3 x 0.20 = 14.66   broker 31/84 bound, blended 34%, incumbent named +5
    urgency          55.0 x 0.15 =  8.25   45 days to effective date
    completeness     90.0 x 0.05 =  4.50   extraction confidence 90
```

**Premium is a triage proxy.** TIV × a rate per $100 by hazard grade, revenue ×
a rate per $1,000. It exists to tell a $9,000 account from a $200,000 one well
enough to sort a queue. It is not a rate, not a quote, and is labelled as an
indication everywhere it surfaces. Where the broker states the expiring
premium, that is used instead — a real number from a real market beats anything
derived from a rate table.

**Winnability is the encodable part of gut feel.** A broker's hit ratio is
shrunk toward the book default by sample size, so four binds out of twelve
reads as twelve tries rather than a 33% record, and a broker with no history
scores the stated default exactly. The reason for shopping is classified from
the broker's prose by keyword: a service complaint converts well, a
non-renewal is a warning, price alone converts poorly.

Two things are scored but kept off the worklist: risks outside appetite, and
risks that belong to the broker who submitted them first. Both keep their full
breakdown. A decline that scored well on everything except appetite is exactly
the evidence you want when asking whether the guide is drawn too tight.

Measured on the corpus, as of the Monday it was generated against: 50
submissions produce 4 accept, 22 refer, 11 pending_info, 13 decline; 35 reach
the worklist and 15 are held back. Every accept lands in the top five.

## Setup

Python 3.10–3.12 and a Google AI Studio API key.

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env    # add your key, or export it in your shell
```

The key is read from `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY` or
`GOOGLE_API_KEY`, whichever is set.

## Usage

```bash
python scripts/generate_mailbox.py --count 50 --seed 7   # synthetic corpus

python -m intake.cli ingest        # read the mailbox
python -m intake.cli parse         # PDF text and document types
python -m intake.cli extract       # the only stage that spends quota
python -m intake.cli clear         # duplicate detection
python -m intake.cli assess        # appetite outcome and the rules that fired
python -m intake.cli score         # the ranked worklist

python -m intake.cli status        # where everything sits
python -m intake.cli show SUB-0043 # one submission in detail
```

`assess` and `score` take `--as-of YYYY-MM-DD` to evaluate against a fixed
date rather than today, which is what the corpus was generated against.
Both take `--rebuild` to discard prior rows and re-run the whole mailbox.

Stage 3 has controls for working against a daily quota:

```bash
python -m intake.cli extract --dry-run          # report the bill, spend nothing
python -m intake.cli extract --limit 5          # cap the calls
python -m intake.cli extract --use-fixtures     # replay, no API at all
python -m intake.cli extract --record-fixtures  # save real responses
```

## The extraction cache

An extract is keyed by a hash of the documents that produced it — filename,
text and readability for each. Change the packet and it re-runs; leave it alone
and it never calls the model again.

That is not an optimisation. One submission costs roughly 50k tokens across the
tool loop, and the free tier allows twenty requests a day on `2.5-flash`.
Without the cache, tuning a scoring weight would re-bill the entire mailbox.

Readability is part of the hash on purpose: if a scanned document later becomes
readable, the extract has to be invalidated rather than kept.

## Scanned documents

A PDF with no text layer yields an empty string, and an empty string reads as
"this document says nothing". Stored silently, that would let a submission
reach an underwriter with its loss history quietly missing.

So a page that produces no text is flagged, the submission is marked for manual
handling, and the agent is told explicitly that an unreadable loss run does not
mean there were no losses. Missing is not the same as favourable. There is no
OCR yet; failing loudly beats mis-extracting a TIV.

## Extraction quality signals

`SubmissionExtract` carries three fields describing the extraction itself:

| Field | Meaning |
|---|---|
| `missing_fields` | Underwriting facts absent from the packet |
| `ambiguities` | Facts that are approximate, conflicting, or asserted without support |
| `extraction_confidence` | 0.0–1.0 completeness and clarity of the packet |

Every substantive field is nullable, so a missing value reaches the rules
engine as missing. The guide decides differently when a fact is absent than
when it is unfavourable.

## Behaviour notes

- **Extraction is deterministic.** `temperature` is pinned to 0.0.
- **Entity names are preserved verbatim.** `Bluewater Mfg. Company` is not
  tidied into `Bluewater Manufacturing Co.` — clearance does its own
  normalisation and needs the original form to do it from.
- **Broker framing is recorded, not applied.** A request to disregard prior
  losses lands in `ambiguities`; the losses are still extracted and still
  evaluated.
- **An estimated value is not a confirmed one.** A TIV the client called
  approximate is passed with `tiv_is_estimated` and does not clear
  completeness — capacity is not committed against an owner's guess. The
  capacity rules still evaluate the figure, so a soft number far above gross
  capacity is still disqualifying.
- **Arrival time is stored as ISO 8601.** The raw `Date` header does not sort:
  as a string, `Fri, 31 Jul` precedes `Mon, 27 Jul`. Clearance decides who owns
  a risk by who submitted first, so getting this wrong would hand accounts to
  the wrong agency silently.
- **Silence is not a zero.** A packet that says nothing about open claims
  yields `None`, not `0` — otherwise the guide would clear a refer threshold
  on a number nobody supplied.
- **Re-running a stage is not a state change.** Landing on the status already
  held is not written to the audit trail, so retuning the weights fifty times
  does not bury the transitions that did happen.

## Tests

```bash
python -m pytest -q
```

No API calls, no network, no mailbox required — the extract fixtures and the
corpus manifest are committed, so a fresh clone can run the whole suite.

`data/corpus_manifest.yaml` records the generated ground truth: which
submissions are duplicates of which, which are decoys, and the true values
behind each packet. Results are checked against it rather than eyeballed.

## Structure

```
src/intake/
  db.py                  SQLite schema, connection, audit events
  models.py              all Pydantic schemas
  appetite.py            rules engine - deterministic, no model
  naics.py               class code resolution
  pipeline/
    ingest.py parse.py extract.py clearance.py assess.py score.py
  agent/
    crew.py tools.py config/{agents,tasks}.yaml
  rules/
    appetite.yaml        the appetite guide
    naics.yaml           class reference and hazard grades
    scoring.yaml         triage weights and premium proxy
  cli.py
scripts/
  generate_mailbox.py    synthetic corpus generator
  synthesize_extracts.py development scaffolding, not the extractor
tests/
  unit/                  rules, cache, clearance, normalisation, scoring
  integration/           full corpus, replayed from fixtures
  fixtures/extracts/     recorded extraction responses
data/mailbox/            generated .eml with real PDF attachments
```

## Synthetic data

`scripts/generate_mailbox.py` writes fifty submissions across twelve brokers,
byte-reproducible for a given seed — which matters, because the extraction
cache keys on content and a regenerated corpus that differed by a byte would
invalidate every fixture.

The corpus deliberately includes what breaks things: incomplete packets, risks
outside appetite, three image-only PDFs with no text layer, three duplicate
pairs written differently by different brokers, and two businesses that share a
name but are not the same risk.

It also has to avoid breaking things by accident. Insured names are rejected if
they come within fuzzy-matching distance of one already used, because an
unplanted near-collision would make the clearance precision figure meaningless
— a flag on that pair would be neither a hit nor a clean miss. Exposure is
sized for the carrier the guide describes: values that routinely cleared the
$10m net line would make "refer for facultative support" the ordinary answer,
which is not what a small carrier's Monday looks like.

Forty-nine of the fifty extracts are currently `fixture:synthetic` —
regex-parsed from the generated corpus rather than read by the model, because
the free-tier quota is twenty requests a day. That provenance is recorded in
the database and printed by `status`, so scaffolding can never be mistaken for
extraction. Re-running `extract` replaces them as quota allows.

## Limitations

- The appetite guide is a working sketch, not a real underwriting manual.
- Loss experience is assessed as incurred against revenue, a proxy used because
  a full premium history is not in these packets.
- The premium proxy sorts a queue and nothing else. Its rate table is the right
  order of magnitude and no more than that.
- Winnability needs bind history a real carrier will not have on day one.
  Stages 1–6 work without it; that component sharpens once outcomes are logged.
- Appetite is evaluated against a single state. A risk with premises in several
  states is judged on the first one listed.
- No OCR. Unreadable documents are detected and flagged, not recovered.
- No real mailbox integration — `.eml` files on disk, so IMAP or Graph drops in
  behind stage 1 without touching anything downstream.
- Synthetic data flatters an extractor. Real ACORD PDFs vary far more.
