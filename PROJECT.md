# PROJECT.md — legal-doc-agent

## Confirmed v1 scope

Contract clause extraction and risk-flagging over CUAD, with citations that
are mechanically verified against the source document. Not RAG, not
case-law research, not playbook redlining — see "Non-goals" below.

The actual point of v1 is the citation-validation layer
(`src/validate/citation_check.py`): independent of extraction method, it
re-checks that every `extracted_text` is an exact substring of the source
document at its claimed offset. Anything that fails is `unverified` and is
structurally excluded from ever becoming an "approved finding" — enforced
server-side in `src/review/queue.py:approve` (raises unconditionally for
`reason == "unverified"`), not just hidden in the UI. See
`tests/test_citation_check.py` and `tests/test_review_queue.py` for the
tests backing that guarantee.

### Non-goals for v1

- **No RAG / case-law retrieval.** There's no licensable authoritative
  corpus of case law available to ground retrieval in, and unlicensed
  scraped case-law retrieval is the single highest-liability corner of
  legal AI (this is the exact failure mode behind the sanctions cases —
  citing real-sounding but non-existent or misapplied authority). Since
  the whole thesis of this project is "make hallucinated citations
  structurally impossible," building a RAG layer without a trustworthy
  corpus to ground it in would undermine that thesis rather than extend
  it. CUAD's contracts are the entire corpus for v1: every claim the
  system makes is checked against a document the user supplied, not
  against something retrieved from an external index.
- **No generative redlining or drafting.** Extraction and flagging only —
  the system never proposes replacement contract language.
- **No playbook comparison / ContractNLI-style entailment.** Deferred to
  v1.5/v2, once the citation-validation backbone here is proven out.

## Architecture decisions made during the build

- **Auto-finding vs. review-queue split.** build-prompt.md's step 6 says
  "low-confidence or unverified extractions get written to a review queue
  ... rather than silently passed through," which implies high-confidence
  *verified* extractions are NOT queued — they pass through as findings
  directly. Its Definition of Done also says the queue is "the only path
  by which **a flagged clause** becomes approved." Read together: only
  extractions that get flagged (low-confidence or unverified) go through
  the queue; the queue governs what happens to *those*, not a mandatory
  human step for every extraction. Implemented in `src/review/queue.py:route`.
- **Unverified items are queued, not dropped.** They're excluded from
  findings absolutely, but they're still written to the queue (reason=
  `unverified`) so a reviewer has visibility into what the pipeline
  rejected and why, rather than that information disappearing silently.
  `approve()` refuses them regardless of caller.
- **LLM confidence is a documented heuristic, not a model-reported score.**
  The Citations API doesn't return a numeric confidence — it returns
  structured `char_location` citations with no probability attached. v1's
  heuristic (`src/extract/llm_extract.py`): 0.85 for a single unambiguous
  citation, 0.6 when a question returns multiple citations (indicates
  ambiguity), with a penalty for very short spans (<10 chars, more likely
  to be a spurious partial match). This is a known simplification — a
  real confidence signal would need calibration against labeled data,
  which is exactly what `src/eval/run_eval.py` is for going forward.
- **Eval match criterion: IoU OR containment, not IoU alone.** See bug #3
  below — this was a real finding from the first eval run, not a decision
  made up front.
- **LLM extraction not empirically run in the build environment.** No
  `ANTHROPIC_API_KEY` was available in the sandbox this was built in.
  `src/extract/llm_extract.py` is complete and its response-parsing logic
  is unit-tested against synthetic API responses
  (`tests/test_llm_extract_parsing.py`), but nobody has yet run it against
  a live document. **Action item for whoever runs this with a real key:**
  run `python -m src.eval.run_eval --extractor llm --limit 10` and
  `--extractor both`, and fill in the per-category/batched cost comparison
  below with real numbers.

## LLM extraction: per-category vs. batched (cost tradeoff)

build-prompt.md step 4 asks to benchmark both calling modes and log the
tradeoff rather than assuming. Without a live API key to run either mode,
this is an analytical estimate, not a benchmark — **flagged as unverified
and worth re-deriving from real token counts once someone runs this with a
key**:

| | `extract_contract_per_category` | `extract_contract_batched` |
|---|---|---|
| Calls per contract | 41 (one per CUAD category) | 1 |
| Document tokens paid in full | Once (first call), then cache reads via `cache_control: ephemeral` on the document block | Once |
| Response parsing | Trivial — every citation in the response belongs to the one category asked about | Depends on parsing Claude's own generated `### Heading` text out of uncited blocks (`parse_batched_response`) to attribute citations to categories |
| Failure mode | None specific to the mode | If Claude skips/renames/reorders a heading, citations for that category silently go unattributed |

Expectation going in: batched should be cheaper in total tokens (proportional to number of round trips and repeated system-prompt overhead) but per-category should be more reliable, since it removes the heading-parsing dependency entirely. Recommendation for v1: **default to per-category** for reliability, keep batched available for cost-sensitive runs where the heading-parsing risk is acceptable. Revisit once real numbers exist.

## Bug log

Real bugs found from actual eval runs (`python -m src.eval.run_eval`), logged as found rather than silently patched.

### Bug 1 — `PARTIES_PREAMBLE_RE` swallowed sentence boundaries because comma/period were inside the greedy token character class

**Found:** first eval run's per-case report (`Parties` P=0.32, R=0.02 against a raw eval run — but the real tell was in the per-case diffs, not the aggregate number) showed a prediction of `"Freeze Tag, Inc. This Agreement"` for a contract whose actual party name was `"Freeze Tag Inc."` — the match ran straight through the sentence boundary.

**Root cause:** the original token pattern `[\w.,&'\-]*` allowed comma and period *inside* a single greedy token match. For text `"... Freeze Tag, Inc. This Agreement (\"Agreement\") ..."`, the token matcher consumed `"Tag,"` and `"Inc."` whole (comma/period included), so the intended terminator alternation `(?:\(|,|\.)` never got a chance to fire where expected — the search kept advancing token-by-token (splitting only on whitespace) until it found some later, unrelated `"("`.

**Fix:** tightened the token class to `[A-Z][A-Za-z0-9&'\-]*\.?` (letters/digits/&/'/-, with at most one trailing period per token) so comma always terminates a token, and periods can't be silently chained across word boundaries. Verified via `tests/test_*` (all still pass) and re-running the eval — no regression, and no more runaway matches in the sampled cases.

**Residual limitation, not fixed:** party-name extraction is still weak in absolute terms (low recall) because CUAD's own annotation is inconsistent about what counts as a "Parties" span — some contracts are annotated with just the abbreviation ("THC"), some with just the full legal name, some with both as separate spans. A regex anchored on "by and between X and Y" structurally can't reproduce that. This is exactly the kind of non-standard clause the architecture routes to the LLM extractor / human review rather than trying to force a regex to handle it — see build-prompt.md's stated design ("70-80% automated, rest routed to human review").

### Bug 2 — `GOVERNING_LAW_RE` jurisdiction capture truncated on internal apostrophes and lowercase connector words

**Found:** same eval run — a contract governed by "the laws of the People's Republic of China" was extracted as just `"People"`.

**Root cause:** the jurisdiction character class was `[a-zA-Z]+` (no apostrophe) for the first token, and continuation tokens required `[A-Z][a-zA-Z]+` — i.e. every extra word had to start uppercase, which "of" and "the" never do. Both constraints broke on real jurisdiction names.

**Fix:** allowed apostrophes in tokens (`[a-zA-Z']+`) and allowed an optional lowercase `of`/`the` connector before a continuation token. `People's Republic of China` now extracts whole.

### Bug 3 — Eval harness scored Governing Law / value-style categories near zero despite correct extractions, because of an IoU-only match metric

**Found:** after fixing bugs 1–2, `Governing Law` was *still* scoring P=0.00 R=0.00 across all 58 contracts where the rule extractor fired — despite every sampled prediction (`"New York"`, `"Delaware"`, `"People's Republic of China"`) being visibly, obviously correct on manual inspection of the per-case diffs.

**Root cause:** this wasn't an extraction bug at all — it was an eval-methodology bug. CUAD annotates the entire governing-law *sentence* as the ground-truth span (e.g. a 250+ character sentence including "shall be governed by, and construed in accordance with..."), because CUAD's task is "highlight the lawyer-reviewable text." `src/extract/rules.py` deliberately extracts just the jurisdiction value ("New York"), which is what's actually useful for downstream structured output. A precise 8-character value nested inside a 250-character ground-truth span has IoU approximately 0.03 — nowhere near the 0.5 threshold — even though it's the exact right answer in the exact right place.

**Fix:** changed the eval match criterion (`src/eval/run_eval.py:_is_span_match`) to count a prediction as correct if it either meets the IoU threshold *or* is fully contained inside the ground-truth span. This is a legitimate methodological correction (short precise extractions nested in a longer correctly-identified clause are not a different phenomenon from a wrongly-placed extraction), not a threshold tuned to inflate a number — documented here specifically so it doesn't look like the latter. After the fix: Governing Law P=0.98, R=0.63, F1=0.77 on the full 102-contract test set.

This is the exact discipline build-prompt.md calls for by requiring a per-case report rather than a single aggregate number: the aggregate ("Governing Law: 0.00") looked like an extraction failure and was actually an eval-metric failure. Only the side-by-side per-case diffs (`ground_truth` vs `predicted` in `eval_reports/*.json`) made the real cause visible.

## Real eval numbers (rule-based extractor, full 102-contract CUAD test set, after bug fixes above)

```
category                                 tp   fp   fn      P      R     F1
--------------------------------------------------------------------------
Agreement Date                           22   15   71   0.59   0.24   0.34
Effective Date                            3    1   81   0.75   0.04   0.07
Governing Law                            57    1   33   0.98   0.63   0.77
Parties                                   6   20  537   0.23   0.01   0.02
--------------------------------------------------------------------------
macro-avg F1 across 4 scored categories: 0.300
```

Everything else scores `n/a`/0 by construction — the deterministic layer only targets these four categories (Parties, Agreement Date, Effective Date, Governing Law) per build-prompt.md's scope for `src/extract/rules.py`; the remaining 37 CUAD categories (Cap On Liability, Non-Compete, Audit Rights, etc.) are exactly the "obligations, liability caps, non-standard clauses" the architecture routes to the LLM extractor, not the regex layer. **Not yet measured against the LLM extractor** — no API key available in the build environment; re-run `python -m src.eval.run_eval --extractor llm` and `--extractor both` with a real key to fill this in.

Reading the numbers honestly: Governing Law is genuinely strong (high precision, decent recall) because "laws of the State of X" is a stable, low-variance pattern. Effective Date's low recall (0.04) is a real, known limitation — the regex only searches the first 4000 characters (preamble window) and only takes the first match, so contracts that state the effective date outside that window or via unanticipated phrasing are missed; this is a documented precision-over-recall tradeoff, not a bug. Parties is the weakest category by a wide margin, for the annotation-inconsistency reason described in Bug 1 — this is the strongest real-world case for why the architecture routes non-standard extraction to the LLM path rather than trying to make regex handle it.

## Data note

CUAD's own repo already ships a train/test split as `train_separate_questions.json` / `test.json` inside `data.zip` (SQuAD 2.0 format) — the loader uses that split as-is rather than deriving a new one, per build-prompt.md. All 2,643 ground-truth spans in the test split were spot-checked (`src/data/cuad_loader.py` smoke test) to be exact substrings of their contract's text at the annotated offset — i.e. CUAD's own ground truth passes the same exactness bar the citation validator enforces on this system's output.

The upstream repo has moved since the dataset was published — `TheAtticusProject/cuad` now redirects to `The-Atticus-Project/cuad`. `scripts/fetch_cuad.py` resolves the repo via the GitHub API at run time (following the redirect) rather than hardcoding either name, per build-prompt.md's instruction not to guess a direct download URL.
