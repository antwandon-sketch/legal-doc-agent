# legal-doc-agent — v1 Build Prompt

Paste this directly into Claude Code, run from inside `~/dev/legal-doc-agent`.

---

## Context

Fourth portfolio piece (AI Engineer / FDE roles), separate from `ai-consulting-lab`,
`claims-triage-agent`, `lead-gen-orchestrator`. Own repo, own venv, `PORT=5004`.
GitHub: `antwandon-sketch/legal-doc-agent` (already created, remote not yet wired —
do that first).

**Confirmed v1 scope:** contract clause extraction and risk-flagging over CUAD,
with citations that are mechanically verified against the source document —
not RAG, not case-law research, not playbook redlining. Those are explicitly
out of scope for v1 (see Non-Goals below).

**Why this architecture:** production legal-AI tooling separates deterministic
extraction (parties, dates, governing law — pattern-matchable, ~90%+ accuracy)
from LLM-based extraction (obligations, liability caps, non-standard clauses —
70-80% automated, rest routed to human review) from a citation-validation layer
that sits outside both and simply checks: does this quoted span literally exist
at this location in the source document? That third layer is the actual point
of v1 — it's what makes citation hallucination structurally impossible for
whatever this system outputs, which is the dominant, sanctions-documented
failure mode in legal AI right now.

---

## Data: CUAD

Contract Understanding Atticus Dataset — 510 real commercial contracts, 13,000+
expert-labeled annotations across 41 clause categories, CC BY 4.0, structured
as extractive QA (span-selection) so ground truth comes built in.

- Source: `https://github.com/TheAtticusProject/cuad` — follow that repo's own
  README for the corpus download (contracts + `CUAD_v1.json`). Don't guess a
  direct download URL; resolve it from the repo at build time.
- Use CUAD's existing train/test split. Eval only ever runs against test.

---

## Build tasks

### 1. Scaffolding
- `venv`, `requirements.txt`, `.env.example` (Anthropic API key placeholder only),
  `.gitignore` (must exclude `.env`, `data/cuad/raw/`, venv dir — confirm with
  `git check-ignore .env` before trusting it)
- `PORT=5004` in `.env.example`
- Wire the GitHub remote: `git remote add origin https://github.com/antwandon-sketch/legal-doc-agent.git`

### 2. Data layer
- `scripts/fetch_cuad.py` — downloads/verifies the CUAD corpus per the repo's
  own instructions, stores under `data/cuad/` (gitignored — don't commit the
  corpus itself)
- Loader that yields `(contract_id, full_text, ground_truth_spans)` per CUAD's
  existing schema

### 3. Deterministic extractors (`src/extract/rules.py`)
Rule/regex-based, no LLM call: party names (signature block + preamble
anchors), effective/execution dates, governing law. These are the fields
that don't need a model.

### 4. LLM structured extraction (`src/extract/llm_extract.py`)
- Use the Anthropic API's **Citations** feature (`citations.enabled` on
  document blocks) rather than prompting Claude to reproduce quotes manually —
  it returns structured `cited_text` spans with character offsets natively,
  which is more reliable and cheaper in output tokens than asking the model to
  echo quotes itself.
- One extraction pass per CUAD clause category (or batched, if you want fewer
  calls — benchmark both, log the tradeoff in PROJECT.md rather than assuming).
- Output validated against a Pydantic schema: `clause_type`, `extracted_text`,
  `source_char_start`, `source_char_end`, `confidence`.
- Default model: `claude-sonnet-5`. Don't hardcode a model string without
  checking current available models if this drifts far in time from today.

### 5. Deterministic citation validator (`src/validate/citation_check.py`)
**This is the non-negotiable core of v1.** Independent of step 4, even though
Citations already returns offsets — the validator re-checks that
`extracted_text` is an *exact* substring of the source document at the claimed
offset. Citations narrows hallucination risk but doesn't eliminate it, so this
check can't be skipped or merged into the extraction step. Anything that fails
gets `validation_status: unverified` and is excluded from anything the system
would present as a finding — no exceptions, no "close enough" fuzzy matching.

### 6. Confidence + review queue (`src/review/queue.py`)
Low-confidence or unverified extractions get written to a review queue
(simple JSON/CSV is fine for v1) rather than silently passed through.

### 7. Minimal review UI (`PORT=5004`)
Thin FastAPI app: list queued extractions, show the flagged text next to its
source-document context, approve/reject. This is the human-approval gate —
nothing from this system should be treated as a finalized "finding" without
passing through it.

### 8. Eval harness (`src/eval/run_eval.py`)
- Per clause-category precision/recall/F1 against CUAD ground truth — never a
  single aggregate accuracy number.
- Per-case report: expected span vs. predicted span, side by side, so a wrong
  case isn't just a statistic. This is the same discipline as
  `lead-gen-orchestrator`'s eval approach — a subtly-wrong citation is worse
  than an obviously-broken one, and only per-case review catches that.

### 9. Bug log
Real bugs found from actual eval runs get logged in PROJECT.md as they're
found, not silently patched — same convention as the other three projects.

### 10. README
Setup, `PORT=5004`, how to run fetch → extract → validate → eval, and a
one-paragraph explanation of why there's no RAG in this system.

---

## Explicit non-goals for v1

- **No RAG / no case-law retrieval.** Confirmed as out of scope — see
  PROJECT.md research notes on why (no licensable authoritative corpus, and
  it's the single highest-liability corner of legal AI).
- **No generative redlining or drafting.** Extraction and flagging only.
- **No playbook comparison / ContractNLI-style entailment.** That's v1.5/v2,
  once the citation-validation backbone is proven here.

---

## Credential hygiene (same as every prior project)
`.env` never pasted into chat, commits, or docs — edit directly in a terminal
text editor. Confirm gitignored with `git check-ignore .env` before trusting
it. `.env.example` gets placeholders only.

---

## Definition of done for v1
- Pipeline runs end-to-end on a batch of CUAD test contracts.
- Eval report shows per-category P/R/F1, with per-case diffs available, not
  just a headline number.
- Review queue is functional and is the only path by which a flagged clause
  becomes "approved."
- The citation validator makes it structurally impossible for an approved
  finding to cite text that isn't actually in the source document — this is
  the one property worth writing a test specifically for.
- PROJECT.md updated with confirmed scope, this build's real bugs, and any
  architecture deviations made during the build.
