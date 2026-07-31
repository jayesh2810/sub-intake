# Submission Triage

Automated intake and triage for commercial insurance submissions. Reads a
broker submission packet — email, ACORD application, statement of values, loss
runs — extracts the underwriting facts, evaluates them against the appetite
guide, and returns a routed decision with a drafted broker reply.

Built on CrewAI with Google Gemini.

## How it works

An intake agent works the packet with four tools:

| Tool | Purpose |
|---|---|
| `list_documents` | See what arrived in the packet |
| `read_document` | Open one document at a time |
| `lookup_naics` | Resolve a class code, or match one from the description of operations |
| `check_appetite` | Evaluate the risk against the appetite guide |

The agent chooses what to open and in what order, so the tool sequence differs
per submission. A packet with a NAICS code on the ACORD resolves the class
directly; a packet without one is matched on the description of operations.

**Appetite is decided in Python, not by the model.** `check_appetite` runs the
rules engine in `appetite.py` against `rules/appetite.yaml` and returns an
outcome with every rule that fired. The agent reports that result verbatim and
is instructed it may not alter it. The model's judgement is confined to reading
messy documents and writing to the broker.

This keeps the decision reproducible, auditable against a named rule, and
testable without spending a token.

## Outcomes

| Outcome | Meaning |
|---|---|
| `accept` | In appetite, proceed to quote |
| `refer` | Requires underwriter review before terms |
| `decline` | Outside appetite as presented |
| `pending_info` | Cannot be evaluated until the broker supplies more |

Precedence when several rules fire: `decline` > `pending_info` > `refer` >
`accept`. A declined risk returns no information request — the file records the
gap for audit, but there is nothing to ask for.

## Requirements

- Python 3.10–3.12
- A Google AI Studio API key

## Setup

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env    # add your key, or export it in your shell
```

The key is read from `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY` or
`GOOGLE_API_KEY`, whichever is set.

## Usage

```bash
.venv/bin/python -m triage.main --list
.venv/bin/python -m triage.main SUB-1001
.venv/bin/python -m triage.main --all
```

Each run prints a triage report and writes the decision plus the full
extraction to `output/<submission_id>.json`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The rules engine is covered end to end without any model calls: class
resolution, outcome precedence, each individual rule, routing, priority
banding, and determinism.

## Structure

```
src/triage/
  models.py              output schemas (SubmissionExtract, TriageDecision)
  appetite.py            rules engine - deterministic, no model
  naics.py               class code resolution
  tools.py               the four agent tools
  crew.py                agent, tasks and crew wiring
  main.py                CLI and reporting
  config/agents.yaml     agent definition
  config/tasks.yaml      extraction and triage instructions
  rules/appetite.yaml    the appetite guide
  rules/naics.yaml       class reference and hazard grades
data/submissions/        input packets, one folder per submission
tests/                   rules engine tests
```

## Extraction quality signals

`SubmissionExtract` carries three fields describing the extraction itself:

| Field | Meaning |
|---|---|
| `missing_fields` | Underwriting facts absent from the packet |
| `ambiguities` | Facts that are approximate, conflicting, or asserted without support |
| `extraction_confidence` | 0.0–1.0 completeness and clarity of the packet |

All substantive fields are nullable. A missing value must reach
`check_appetite` as missing, because the guide decides differently when a fact
is absent than when it is unfavourable.

## Behaviour notes

- **Extraction is deterministic.** `temperature` is pinned to 0.0.
- **Entity names are preserved verbatim.** `Bluewater Mfg. Company` is not
  normalised to `Bluewater Manufacturing Co.` — clearance matching depends on
  the original form.
- **Broker framing is recorded, not applied.** A request to disregard prior
  losses lands in `ambiguities`; the losses are still extracted and still
  evaluated.
- **An estimated value is not a confirmed one.** A TIV the client called
  approximate is passed with `tiv_is_estimated`, and does not clear
  completeness — capacity is not committed against an owner's guess. The
  capacity rules still evaluate the figure, so a soft number far above gross
  capacity is still disqualifying.
- **The broker reply never quotes terms.** No premium, rate or capacity figure.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | API credential |
| `TRIAGE_MODEL` | `gemini/gemini-2.5-flash` | LiteLLM model identifier |
| `TRIAGE_MAX_RPM` | `4` | Request throttle |

### Free tier quotas

The tool loop issues roughly ten requests per submission. Google's free tier
limits are per model per day, and they are low enough to matter:

| Model | Free tier requests/day |
|---|---|
| `gemini-2.5-flash` | 20 |
| `gemini-2.5-flash-lite` | substantially higher |

Twenty requests per day is about two submissions. For batch runs use:

```bash
TRIAGE_MODEL=gemini/gemini-2.5-flash-lite TRIAGE_MAX_RPM=8 \
  .venv/bin/python -m triage.main --all
```

Quota is tracked per model, so exhausting one leaves the others available.

## Sample data

`data/submissions/` contains four synthetic packets covering the common intake
cases: a complete manufacturing account, an incomplete habitational account
with no loss runs, a duplicate of the manufacturing account from a second
broker, and a contractor account with adverse loss history and a prior
non-renewal.

## Current limitations

- Plain text input only; PDF and email attachment parsing is not implemented.
- The appetite guide is a working sketch, not a real underwriting manual.
- Loss experience is assessed as incurred against revenue, a proxy used because
  a full premium history is not available in these packets.
- No persistence between runs — each submission is processed independently, and
  a duplicate submission is re-processed from scratch.

## Roadmap

- Persisted submission state and lifecycle transitions
- Clearance against previously received submissions
