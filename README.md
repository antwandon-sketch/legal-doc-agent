# legal-doc-agent

Contract clause extraction and risk-flagging over [CUAD](https://github.com/The-Atticus-Project/cuad)
(the Contract Understanding Atticus Dataset), built around one claim, made
mechanically true rather than asserted: **this system cannot present a
"finding" whose quoted text doesn't really exist at the claimed location in
the source document.**

That's the whole point of v1. Every legal-AI hallucination scandal follows
the same shape — a system asserted something that sounded like a real
citation without a mechanism forcing it to point at something that's
actually there. `src/validate/citation_check.py` sits outside both
extraction paths (regex and LLM) and independently re-checks every
`extracted_text` as an exact substring of the source document at its
claimed offset. Anything that fails is `unverified`, and `src/review/queue.py:approve`
refuses — unconditionally, server-side — to ever let an unverified item
become an approved finding, regardless of what a UI or a stale page might
try to POST. See `tests/test_citation_check.py` for the tests backing that
guarantee, and the [adversarial demo](#adversarial-demo) below for it
exercised live against a real API response.

**What this guarantee does *not* cover:** it verifies *text-at-offset*,
not *category-correctness*. A citation can be genuinely, verifiably present
in the document and still be the wrong answer — real text quoted against
the wrong clause category. That gap is documented and measured in
[Bug #5](PROJECT.md#bug-5--llm-extractor-has-systematically-low-precision-on-categories-a-contract-doesnt-address-because-the-per-category-prompt-doesnt-stop-claude-from-quoting-topically-adjacent-but-wrong-text-instead-of-answering-none-found)
in `PROJECT.md`, mitigated (not fixed) by routing low-confidence,
multi-citation extractions to human review instead of auto-approving them.
The [adversarial demo](#adversarial-demo) shows both halves side by side —
category-confusion the validator correctly lets through as `verified`
(because the text really is there), and a corrupted offset the validator
correctly blocks.

This is not RAG, not case-law research, and not playbook redlining — see
[Why no RAG](#why-no-rag) below and `PROJECT.md` for the full non-goals
list and architecture notes.

## Final v1 numbers

- **Rule-based extractor, full 102-contract CUAD test set** (4 categories:
  Parties, Agreement Date, Effective Date, Governing Law — the only ones
  with low-variance phrasing a regex can reliably target): **macro F1 0.300**.
  Governing Law is the strongest (P=0.98, R=0.63, F1=0.77 — "laws of the
  State of X" is a stable pattern). Parties is the weakest (F1=0.02),
  for a documented non-bug reason: CUAD's own annotation is inconsistent
  about what counts as a "Parties" span.
- **LLM extractor, most representative run** — 18-contract stratified
  sample (built by greedy set-cover to reach 40/41 possible CUAD
  categories, rather than the `n/a`-heavy alphabetical-first-N samples used
  earlier in the build), standing few-shot abstention prompt, hybrid
  warm-up + Batch API extraction: **tp=389, fp=855, fn=499 — P=0.313,
  R=0.438, macro F1=0.398** across 40 scored categories. This is the number
  to cite for "how good is the LLM extractor" — earlier 10-contract runs
  under-covered categories badly enough to leave many at `n/a` by sampling
  accident.
- **Head-to-head on the 4 shared categories** (10-contract sample): LLM
  beats rule-based on Effective Date (F1 0.35 vs. unscored 0.00-recall) and
  Governing Law (F1 1.00 vs. 0.67); rule-based beats LLM on Agreement Date
  (F1 0.43 vs. LLM's 0.00 — a value-style over-quoting problem); both are
  weak on Parties, same CUAD-annotation reason as above.

Full per-category tables, every bug that produced these numbers, and the
Batch API cost experiment (real cost came in 4x over the optimistic
estimate — a genuine platform finding, not a code bug) are in `PROJECT.md`.

**Non-determinism caveat:** `claude-sonnet-5` and the whole Claude 5 /
4.6+ model family reject `temperature`/`top_p`/`top_k` outright — there is
no supported way to pin sampling determinism on this model generation.
Every LLM-extractor comparison above and in `PROJECT.md` is therefore a
single run at the API's non-deterministic default, not a controlled,
repeatable A/B. This is a standing property of the model this project runs
on, not a gap a future run can close by trying harder.

## Adversarial demo

`demos/adversarial_citation_demo.py` makes the citation-validation
guarantee concrete instead of asserted, in two real code-path acts (no
mocking):

```bash
python demos/adversarial_citation_demo.py            # needs ANTHROPIC_API_KEY
python demos/adversarial_citation_demo.py --no-live   # skips the live call, runs Act 2 only
```

- **Act 1 (real model attempt):** asks the live LLM extractor about CUAD
  categories a real contract genuinely doesn't contain, in descending order
  of the false-positive rate Bug #5 documents. It reliably reproduces Bug
  #5 live — real contract text, at a correct offset, just not evidence of
  the category asked about. `citation_check.py` correctly marks this
  `verified` (the text really is there); the confidence gate, not the
  validator, is what keeps it out of auto-approved findings.
- **Act 2 (deliberately corrupted span):** takes the same real citation and
  shifts its offset by one character — the actual shape of hallucination
  the validator exists to catch. Comes back `unverified`, gets queued with
  `reason="unverified"`, and a simulated `approve()` call on it raises
  unconditionally.

A captured real run is saved at `demos/sample_output.txt`.

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
contract/category pair. See `PROJECT.md` for the bugs that run surfaced and
the full numbers behind the summary above.

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

v1, closed out. See `PROJECT.md` for the full bug log (every one found from
a real eval run, none from inspection alone), architecture decisions made
during the build, and where this deviates from a literal reading of the
original build prompt and why.
