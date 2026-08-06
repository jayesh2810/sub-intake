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

Stages 1–4 are built. Each stage is idempotent, independently runnable and
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

python -m intake.cli status        # where everything sits
python -m intake.cli show SUB-0046 # one submission in detail
```

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
    ingest.py parse.py extract.py clearance.py
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
  unit/                  rules, cache, clearance, normalisation
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

Forty-nine of the fifty extracts are currently `fixture:synthetic` —
regex-parsed from the generated corpus rather than read by the model, because
the free-tier quota is twenty requests a day. That provenance is recorded in
the database and printed by `status`, so scaffolding can never be mistaken for
extraction. Re-running `extract` replaces them as quota allows.

## Limitations

- The appetite guide is a working sketch, not a real underwriting manual.
- Loss experience is assessed as incurred against revenue, a proxy used because
  a full premium history is not in these packets.
- No OCR. Unreadable documents are detected and flagged, not recovered.
- No real mailbox integration — `.eml` files on disk, so IMAP or Graph drops in
  behind stage 1 without touching anything downstream.
- Synthetic data flatters an extractor. Real ACORD PDFs vary far more.
