# Submission Triage

Automated intake for commercial insurance submissions. Reads a broker
submission packet — email, ACORD application, statement of values, loss runs —
and returns validated structured data for downstream underwriting.

Built on CrewAI with Google Gemini.

## Requirements

- Python 3.10–3.12
- A Google AI Studio API key

## Setup

```bash
uv venv
uv pip install "crewai[google-genai]" pydantic python-dotenv
cp .env.example .env    # add your key
```

The key is read from `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY` or
`GOOGLE_API_KEY`, whichever is set.

## Usage

```bash
PYTHONPATH=src .venv/bin/python -m triage.main --list
PYTHONPATH=src .venv/bin/python -m triage.main SUB-1001
PYTHONPATH=src .venv/bin/python -m triage.main --all
```

Each run prints an underwriter summary and writes the full extraction to
`output/<submission_id>.json`.

```
----------------------------------------------------------------------
  SUB-1001: Bluewater Manufacturing Co.
----------------------------------------------------------------------
  Broker         Harborline Insurance Brokers / Devanshi Patel
  Operations     Precision metal stamping and sheet metal fabrication
  NAICS          332119
  Revenue        $22,400,000
  Employees      140
  TIV            $14,235,000
  Locations      2
  Effective      2026-09-01
  Coverages      COMMERCIAL PROPERTY, COMMERCIAL GENERAL LIABILITY
  Losses         $144,650 over 10 entries (carrier loss runs attached)

  Confidence     0.90
```

## Structure

```
src/triage/
  models.py              output schema (SubmissionExtract)
  crew.py                agent, task and crew wiring
  main.py                CLI, document loading, reporting
  config/agents.yaml     agent definition
  config/tasks.yaml      extraction instructions
data/submissions/        input packets, one folder per submission
output/                  extraction results as JSON
```

## Output

`SubmissionExtract` covers broker and insured details, requested coverages,
locations and values, loss history, and incumbent market information. All
substantive fields are nullable.

Three fields describe the quality of the extraction itself:

| Field | Meaning |
|---|---|
| `missing_fields` | Underwriting facts absent from the packet |
| `ambiguities` | Facts that are approximate, conflicting, or asserted without support |
| `extraction_confidence` | 0.0–1.0 completeness and clarity of the packet |

Downstream consumers should gate on `extraction_confidence` and treat a
non-empty `missing_fields` as a follow-up trigger rather than a failure.

## Behaviour notes

- **Extraction is deterministic.** `temperature` is pinned to 0.0 so the same
  packet yields the same values on every run.
- **Entity names are preserved verbatim.** `Bluewater Mfg. Company` is not
  normalised to `Bluewater Manufacturing Co.`. Clearance matching depends on
  the original form.
- **Broker framing is recorded, not applied.** A request to disregard prior
  losses lands in `ambiguities`; the losses are still extracted in full.
- **No decision is made here.** Appetite, pricing and routing are evaluated
  downstream against the appetite guide, not by the model.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | API credential |
| `TRIAGE_MODEL` | `gemini/gemini-2.5-flash` | LiteLLM model identifier |

## Sample data

`data/submissions/` contains four synthetic packets covering the common intake
cases: a complete manufacturing account, an incomplete habitational account
with no loss runs, a duplicate of the manufacturing account from a second
broker, and a contractor account with adverse loss history and a prior
non-renewal.

## Current limitations

- Plain text input only; PDF and email attachment parsing is not implemented.
- Documents are loaded by the CLI, not selected by the agent.
- No persistence between runs — each submission is processed independently.
- Extracted values are not reconciled against external data sources.

## Roadmap

- File-reading and lookup tools, with appetite evaluated by a deterministic
  rules engine
- Persisted submission state and lifecycle transitions
- Clearance against previously received submissions
