# legal-doc-agent

Contract clause extraction and risk-flagging over [CUAD](https://github.com/The-Atticus-Project/cuad)
(the Contract Understanding Atticus Dataset), with citations that are
**mechanically verified** against the source document rather than trusted
because a model returned them.

This is not RAG, not case-law research, and not playbook redlining — see
[Why no RAG](#why-no-rag) below and `PROJECT.md` for the full non-goals
list and architecture notes.

## Architecture

```
CUAD contract text
       │
       ├─► src/extract/rules.py      (deterministic: parties, dates, governing law — no LLM)
       └─► src/extract/llm_extract.py (LLM + Anthropic Citations API: everything else)
                       │
                       ▼
       src/validate/citation_check.py
       (independent re-check: is extracted_text an EXACT substring
        of the source document at the claimed offset? no exceptions)
                       │
              ┌────────┴────────┐
              ▼                 ▼
      verified + high      unverified, or
      confidence           low confidence
              │                 │
              ▼                 ▼
      auto-finding      src/review/queue.py
                         (human approve/reject —
                          unverified items can
                          NEVER be approved,
                          enforced server-side)
                                 │
                                 ▼
                    src/app/main.py (review UI, PORT=5004)
```

The citation validator is the actual point of this project: it sits
outside both extraction paths and makes it structurally impossible for
this system to present a "finding" whose quoted text doesn't really exist
at the claimed location in the source document — see
`tests/test_citation_check.py`.

## Setup

Requires Python 3.11+ (built and tested against 3.12).

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env in a terminal text editor and add your ANTHROPIC_API_KEY —
# never paste it into chat, commits, or docs
git check-ignore .env   # confirm it prints ".env" before trusting it's gitignored
```

`.env` sets `PORT=5004` (the review UI) and `ANTHROPIC_MODEL` (defaults to
`claude-sonnet-5`).

## Running the pipeline

**1. Fetch the CUAD corpus** (downloads `data.zip` from the CUAD repo,
resolved dynamically via the GitHub API since the repo has moved before;
stored under `data/cuad/raw/`, gitignored):

```bash
python scripts/fetch_cuad.py
```

**2. Run extraction → validation → routing** on a batch of test contracts:

```bash
python scripts/run_pipeline.py --limit 25
# add --use-llm to also run the LLM extractor (needs ANTHROPIC_API_KEY)
```

This writes auto-approved findings to `findings.json` and appends
low-confidence/unverified extractions to `review_queue/queue.json`.

**3. Review the queue:**

```bash
python scripts/run_review_ui.py
# open http://127.0.0.1:5004/queue
```

Approve or reject each item. Unverified items (citation check failed) are
shown for visibility but their Approve button is inert — the backend
refuses the approval regardless of what the UI does, so this can't be
bypassed by a stale page or a direct POST.

**4. Run the eval harness** (per-category precision/recall/F1 against CUAD
ground truth, plus a full per-case JSON report):

```bash
python -m src.eval.run_eval --extractor rule --limit 102   # full test set, no API key needed
python -m src.eval.run_eval --extractor llm --limit 10      # needs ANTHROPIC_API_KEY
python -m src.eval.run_eval --extractor both --limit 10
```

Prints a per-category table (never a single aggregate accuracy number) and
writes `eval_reports/eval_<extractor>_<n>.json` with the full per-case
breakdown — expected span vs. predicted span, side by side, for every
contract/category pair. See `PROJECT.md` for real numbers from a full run
against the rule-based extractor and the bugs that run surfaced.

**Tests:**

```bash
pytest
```

`tests/test_citation_check.py` is the one test suite specifically proving
the structural guarantee described above (exact match required, offset
shifts fail, out-of-bounds offsets fail safely, unvalidated clauses are
never treated as findings).

## Why no RAG

Grounding legal-AI output in retrieval only helps if the retrieval corpus
itself is trustworthy and complete — and for case law, no licensable,
comprehensive, authoritative corpus is available to build that on. Every
sanctions case involving hallucinated legal citations follows the same
shape: a system asserted something that sounded like a real citation
without a mechanism forcing it to point at something that's actually
there. Since the whole thesis of this project is making that failure mode
structurally impossible, bolting a RAG layer onto an ungrounded or
partial case-law index would work against that thesis rather than extend
it — it would just move the unverifiable claim from "the model's
memorized training data" to "the model's memorized training data plus an
index I can't fully vouch for." CUAD's contracts are the entire
document universe for v1: every claim this system makes is checked
against a document the user actually supplied, never against something
retrieved from an external source. Case-law retrieval is explicitly
deferred, not ruled out forever — it's a v2+ problem once there's a
corpus worth grounding it in.

## Status

v1. See `PROJECT.md` for confirmed scope, the real bug log from actual
eval runs, and architecture decisions made during the build (including
where this deviates from a literal reading of the original build prompt
and why).
