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
- **LLM extraction run against the live API (2026-08-11).** Ran
  `python -m src.eval.run_eval --extractor llm --limit 10` against real
  CUAD test-set contracts with a live `ANTHROPIC_API_KEY`. This
  immediately surfaced a real crash (bug #4 below, now fixed) — the
  unit tests in `tests/test_llm_extract_parsing.py` only ever exercised
  synthetic dicts, never the actual SDK response objects the
  per-category path receives, so the parsing bug shipped invisibly
  until it hit a live document. After the fix, full 10-contract results
  and a second real finding (bug #5, a precision problem, not a crash)
  are below in "Real eval numbers (LLM extractor...)".

## LLM extraction: per-category vs. batched (cost tradeoff)

build-prompt.md step 4 asks to benchmark both calling modes and log the
tradeoff rather than assuming.

| | `extract_contract_per_category` | `extract_contract_batched` |
|---|---|---|
| Calls per contract | 41 (one per CUAD category) | 1 |
| Document tokens paid in full | Once (first call), then cache reads via `cache_control: ephemeral` on the document block | Once |
| Response parsing | Trivial — every citation in the response belongs to the one category asked about | Depends on parsing Claude's own generated `### Heading` text out of uncited blocks (`parse_batched_response`) to attribute citations to categories |
| Failure mode | None specific to the mode | If Claude skips/renames/reorders a heading, citations for that category silently go unattributed |
| Observed wall-clock (live run, 2026-08-11) | ~170s/contract (41 sequential calls, no batching/parallelism in `extract_contract_per_category`'s implementation — it's a plain `for` loop) | not yet run |

**Still not run: `--extractor both`/`extract_contract_batched` against a live key**, so the token/cost columns are still the analytical estimate from the original writeup, not measured — this run didn't instrument per-call `usage` (cache_creation_input_tokens/cache_read_input_tokens), so even the per-category token cost isn't quantified yet, only the calls-per-contract count and wall-clock time. What *did* get measured: per-category latency is real and non-trivial (~4.2s/call average, all sequential) — for a corpus-wide run (102 contracts × 41 calls = 4,182 calls) that's ~5 hours wall-clock at this rate, which is itself an argument for parallelizing the per-category loop (independent calls, trivially parallelizable) before it's a strong recommendation to "default to per-category," separate from the reliability argument.

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

### Bug 4 — `extract_contract_per_category` crashed on every real API response with citations, because citation objects were treated as dicts

**Found:** the very first live-key run (`python -m src.eval.run_eval --extractor llm --limit 1`) crashed on the first contract that got any citation back at all:
`AttributeError: 'CitationCharLocation' object has no attribute 'get'`, raised from `src/extract/llm_extract.py:_citations_to_clauses`.

**Root cause:** `_citations_to_clauses` was written against dict-shaped citations (`cit.get("type")`, `cit["cited_text"]`, `cit["start_char_index"]`, `cit["end_char_index"]`) — which is exactly what `extract_contract_batched` hands it, because `parse_batched_response` calls `.model_dump()` on every citation before storing it. But `extract_contract_per_category` passes citations straight from `response.content[i].citations` with no such conversion — those are live `anthropic.types.CitationCharLocation` pydantic objects, which support attribute access, not `dict`-style `.get()`/`[]`. `tests/test_llm_extract_parsing.py` never caught this because it only unit-tests `parse_batched_response` (the batched path) with synthetic dicts; nothing exercised `_citations_to_clauses` against real SDK objects, and per-category is the default/recommended mode (see cost-tradeoff section above). This is exactly the gap PROJECT.md already flagged: "nobody has yet run it against a live document."

**Fix:** added `_citation_field(cit, name)`, which accepts either a dict or an object (`isinstance(cit, dict)` → `.get`, else `getattr`) — the same dict-or-object tolerance `parse_batched_response` already used for blocks/citations, just applied consistently in `_citations_to_clauses` too. `extract_contract_per_category` needed no changes; existing unit tests still pass unmodified.

### Bug 5 — LLM extractor has systematically low precision on categories a contract doesn't address, because the per-category prompt doesn't stop Claude from quoting topically-adjacent-but-wrong text instead of answering "None found."

**Found:** live 10-contract run — categories where CUAD says *every* contract in the sample lacks the clause still came back with double-digit false positives: `Third Party Beneficiary` (0 tp / 17 fp), `Non-Transferable License` (0 tp / 20 fp), `Volume Restriction` (0 tp / 18 fp), `Post-Termination Services` (6 tp / **41** fp), `Affiliate License-Licensee` (1 tp / 17 fp). All of these predictions are `validation_status: verified` (genuinely exact substrings of the contract, confidence 0.6–0.85) — this isn't hallucinated text, it's real contract language attributed to the wrong category.

**Root cause, with a concrete example:** for the Dova/Valeant co-promotion contract, asking about `Post-Termination Services` returned a citation whose actual text begins "13.11 Third Party Beneficiaries. Except as set forth in ARTICLE 11, no Person other than Dova or Val[eant]..." — i.e. Claude quoted a *disclaimer* clause (explicitly denying third-party beneficiary rights) as an answer to an unrelated category. More generally, the per-category prompt (`"{question}\n\nQuote only the exact contract text that answers this. If nothing in the contract answers it, reply with exactly: \"None found.\""`) gives Claude no incentive to prefer silence over a plausible-looking nearby match, so when a contract is short on genuinely relevant text, it reaches for the closest thematically-related clause (indemnification/survival boilerplate for "Post-Termination Services," delivery-penalty clauses for "Cap On Liability," etc.) rather than concluding the category doesn't apply.

**Not fixed on the first attempt — this is a prompting/precision problem, not a code defect**, and needs iteration against labeled data rather than a one-line patch: candidates worth trying were (a) explicitly telling Claude that a topically-related-but-non-responsive clause should still get "None found.", (b) few-shot examples of correct abstention, or (c) a stricter downstream filter using the existing 0.6 "multi-citation" confidence signal (`CONFIDENCE_MULTI_CITATION`) — several of the worst offenders above (`Post-Termination Services`, `Third Party Beneficiary`) return multiple citations per contract, which is already the signal the codebase uses elsewhere to mean "ambiguous." **(a) was tried — see "Bug 5 resolution attempt" immediately below. It did not fix the problem.**

### Bug 5 resolution attempt (2026-08-11) — explicit `NOT_FOUND` abstention instruction: tried, did not fix it

**Change made:** `extract_contract_per_category`'s prompt (`src/extract/llm_extract.py`) was rewritten from a bare "quote it or say None found" instruction to explicitly warn against exactly the failure mode found above:

> "Quote only the exact contract text that directly and specifically answers this question. Do not quote text that is merely on the same general topic but does not itself answer the question — for example, a clause that disclaims or negates a right is not an answer to a question asking whether that right is granted, and a clause about a related-but-different obligation is not an answer either. If the contract does not address this category at all, or only contains text that is thematically related but doesn't actually answer the question, reply with exactly: NOT_FOUND"

No parsing code changed — `extract_contract_per_category` only ever looks at the `citations` array on the response, never the response's plain text, so whether Claude says "None found." or "NOT_FOUND" was already a no-op for the code path; the only thing that could change is whether Claude *attaches a citation at all*. This isolates the test to the actual variable of interest (does clearer instruction reduce spurious citations) rather than accidentally testing a parsing change too.

**Rerun:** identical command, same 10 contracts, same model (`claude-sonnet-5`), same `extract_contract_per_category` mode — `python -m src.eval.run_eval --extractor llm --limit 10 --out eval_reports/eval_llm_10_live_v2_abstention.json`.

**Aggregate result, all 41 categories, before vs. after:**

| | tp | fp | fn | precision | recall | macro-F1 |
|---|---|---|---|---|---|---|
| Before (bug #5 as found) | 120 | 384 | 143 | 0.238 | 0.456 | 0.381 |
| After (`NOT_FOUND` instruction) | 105 | 345 | 158 | 0.233 | 0.399 | 0.350 |

**Honest verdict: this did not fix Bug 5, and by the aggregate numbers it's a net regression, not an improvement.** False positives did drop (384 → 345, ~10%), but true positives dropped by more in proportion (120 → 105), so precision didn't move (0.238 → 0.233 — statistically a wash) while recall fell by a real margin (0.456 → 0.399) and macro-F1 fell (0.381 → 0.350).

Breaking down *why* it's not a clean win, not just a wash:

- **Some categories improved exactly as hoped** — `Third Party Beneficiary` fp 17→8, `Uncapped Liability` fp 22→15, `Volume Restriction` fp 18→13, `Affiliate License-Licensor` fp 21→16, all with tp unchanged (0 in most of these, since ground truth is empty for this sample) — i.e. real reductions in spurious citations with no cost to recall, the intended effect.
- **But several categories got *worse* on false positives despite the new instruction**, which is the counter-intuitive part: `Document Name` fp 2→10, `Effective Date` fp 9→13, `Agreement Date` fp 6→9, `Covenant Not To Sue` fp 0→3. These are the value-style categories flagged in Bug 5's "distinguish from Bug 3" paragraph as suffering from over-broad quoting, not topical confusion — the new instruction targets the wrong failure mode for this category class, and apparently made Claude more verbose/hedging on them rather than more precise.
- **And the instruction suppressed some previously-correct extractions**, not just wrong ones — true positives fell in categories that were already scoring reasonably: `Insurance` tp 9→4, `Cap On Liability` tp 10→7, `Post-Termination Services` tp 6→2, `Competitive Restriction Exception` tp 4→2. This is the actual mechanism behind the recall drop: the more conservative wording didn't just suppress adjacent-clause confusion, it also made Claude more willing to abstain on categories it previously got right.

**Methodological caveat, in the interest of not overclaiming a negative result either:** neither run pinned `temperature` (the API default applies, not 0), so a single run per condition on a 10-contract sample carries real sampling noise on top of whatever the prompt change did — this is not a controlled A/B in the statistical sense. That said, the direction and category-level pattern (fixes exactly where expected, regressions exactly on the category class the fix wasn't aimed at, plus new suppression of correct answers) is a large enough and structured enough effect that it reads as a real prompt-sensitivity result, not pure noise — but a rigorous confirmation would rerun both conditions at `temperature=0` (or average several runs) before treating either table as ground truth.

**Status after attempt 1: still open**, see attempt 2 immediately below for the final disposition.

**Distinguish from Bug 3:** Bug 3 was purely an eval-metric gap (a correct, precise extraction failing to match an intentionally coarser ground-truth span) and was fixed by loosening the metric. This is the reverse shape but is *not* the same kind of bug — for value-style categories (`Agreement Date`, `Document Name`), Claude's citations are consistently *larger* than CUAD's ground truth (e.g. quoting "Exhibit 10.4 INTELLECTUAL PROPERTY AGREEMENT This INTELLECTUAL PROPERTY AGREEMENT (this..." as the answer to `Document Name`, where the ground truth is just `"INTELLECTUAL PROPERTY AGREEMENT"`, nested inside it). Symmetrically loosening `_is_span_match` to also credit ground-truth-inside-prediction would make these score as correct — but that would be *hiding* a real quality problem, not correcting a metric artifact: a "Document Name" field that's actually a full paragraph of boilerplate is not usable as structured output, unlike Bug 3's case where the tighter span was exactly the desired product behavior. So `run_eval.py` was deliberately left unchanged here; the fix belongs in prompting (ask explicitly for the minimal quoted span for value-style categories), not in the eval.

### Bug 5 resolution attempt 2 (2026-08-12) — few-shot abstention examples, plus a `temperature=0` detour that turned out to be impossible

**Also attempted first: pinning `temperature=0`** so this and the prior two runs would be a controlled comparison rather than three independently-sampled runs. This immediately 400'd against the live API: `temperature is deprecated for this model`. `claude-sonnet-5` is part of the Claude 5 / 4.6+ model generation, which removed `temperature`/`top_p`/`top_k` from the request surface entirely — there is no supported way to pin sampling determinism on this model family; the parameter has to be omitted, not set to a specific value (including `0`). This is a real, load-bearing finding in its own right: **all three LLM-extractor comparisons in this doc (bug #5 baseline, resolution attempt 1, resolution attempt 2) are single runs at the API's default, non-deterministic sampling, not a controlled A/B/C** — a caveat already flagged for attempt 1, now confirmed permanent rather than a temporary gap to close. `src/extract/llm_extract.py` was left with no `temperature` argument at all (not even `1`, the default) and a comment explaining why, rather than silently dropping the parameter with no trace of the attempt.

**Change made:** added 3 few-shot examples to the same `extract_contract_per_category` prompt from attempt 1 (the `NOT_FOUND` instruction was kept), designed specifically to fix what attempt 1 got wrong — attempt 1 suppressed some *correct* extractions along with the incorrect ones, suggesting the bare instruction just made the model generally more cautious rather than precisely targeting the disclaimer/topical-confusion failure mode. The examples pair two abstention cases (a disclaiming clause misread as a granting clause; a topically-adjacent-but-different clause) with **one positive example showing correct quoting still happens** — included deliberately, so the model isn't shown abstention as the only pattern:

> 1. Question about "Third Party Beneficiary" rights being granted. Contract says: "no Person other than the parties hereto shall have any right under this Agreement." → correct answer: NOT_FOUND (disclaims, doesn't grant).
> 2. Question about "Post-Termination Services." Contract says only: "confidentiality obligations shall survive termination." → correct answer: NOT_FOUND (survival clause, not a service obligation).
> 3. Question about "Governing Law." Contract says: "governed by the laws of the State of Delaware." → correct answer: quote it — don't withhold a genuine answer just because the examples above show abstention.

**Rerun:** same 10 contracts, same model, no `temperature` (see above) — `eval_reports/eval_llm_10_live_v3_fewshot.json`. (First attempt at this rerun hit a transient `httpx.ReadTimeout` after the SDK's retry budget was exhausted — a one-off network flake, not a product or model issue; a plain retry of the identical command succeeded.)

**Three-way aggregate comparison, all 41 categories:**

| | tp | fp | fn | precision | recall | macro-F1 |
|---|---|---|---|---|---|---|
| Baseline (bug #5 as found) | 120 | 384 | 143 | 0.238 | 0.456 | 0.381 |
| Attempt 1 (`NOT_FOUND` instruction only) | 105 | 345 | 158 | 0.233 | 0.399 | 0.350 |
| Attempt 2 (`NOT_FOUND` + few-shot) | 109 | 258 | 154 | 0.297 | 0.414 | 0.404 |

Attempt 2 clearly beats attempt 1 on every metric (fp down another 25%, precision up, recall up, F1 up) — the few-shot examples did what the bare instruction alone couldn't: fp dropped broadly and consistently across categories (`Uncapped Liability` 22→10, `Affiliate License-Licensor` 21→8, `Affiliate License-Licensee` 17→3, `Revenue/Profit Sharing` 18→8, `Liquidated Damages` 9→2, `Parties` 18→10, `Third Party Beneficiary` 17→8), without attempt 1's pattern of *new* fp regressions in unrelated categories.

**But against the original baseline it's a real tradeoff, not a clean win.** Precision improved substantially (0.238 → 0.297, +25% relative) and macro-F1 improved (0.381 → 0.404), but recall is still down (0.456 → 0.414, −9% relative) — fewer categories lost true positives than in attempt 1 (`Anti-Assignment`, `Cap On Liability`, `Exclusivity`, `Insurance`, `Post-Termination Services`, `Renewal Term`, `Warranty Duration` each dropped 1-3 tp), but the drop wasn't eliminated, just reduced.

**Mitigating context worth weighing before calling this a production risk:** of the 258 remaining false positives, 205 (79%) carry `confidence: 0.6` (the multi-citation heuristic) — below `CONFIDENCE_THRESHOLD` (0.75, `src/config.py`), meaning `src/review/queue.py`'s routing already sends these to the human review queue rather than auto-approving them as findings (see PROJECT.md's "Auto-finding vs. review-queue split" architecture note). Only the remaining 53 false positives (single-citation, `confidence: 0.85`) would bypass review and reach a user as an unverified "finding" — the actual exposure from this bug is roughly a fifth of the raw false-positive count, not all of it.

**Final verdict, per the explicit stopping rule for this iteration: does not clearly improve on both the baseline and attempt 1 — it improves cleanly on attempt 1, but only partially on baseline (precision/F1 up, recall down).** Per plan, this is where iteration stops rather than trying a third prompt variant chasing the recall gap.

**Status: Bug 5 is closed for v1 as a known limitation, not fixed.** The `NOT_FOUND` instruction plus few-shot examples are left in place (they're a net improvement over the original bare-instruction prompt and over doing nothing on precision), but this is documented as a real, un-closed precision/recall tradeoff in the LLM extractor, not a resolved bug. Recommendation for v1.5/v2, if this is revisited: the two remaining candidates from the original list — post-hoc filtering on the confidence signal, and a `temperature`-independent multi-run average to separate real prompt effects from sampling noise — should be tested independently of further prompt rewording, since this round's evidence suggests prompt-only tuning is running into a real ceiling (precision and recall pulling in opposite directions on the same instruction) rather than an unexplored direction.

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

Everything else scores `n/a`/0 by construction — the deterministic layer only targets these four categories (Parties, Agreement Date, Effective Date, Governing Law) per build-prompt.md's scope for `src/extract/rules.py`; the remaining 37 CUAD categories (Cap On Liability, Non-Compete, Audit Rights, etc.) are exactly the "obligations, liability caps, non-standard clauses" the architecture routes to the LLM extractor, not the regex layer. See below for the LLM extractor's numbers on those categories.

Reading the numbers honestly: Governing Law is genuinely strong (high precision, decent recall) because "laws of the State of X" is a stable, low-variance pattern. Effective Date's low recall (0.04) is a real, known limitation — the regex only searches the first 4000 characters (preamble window) and only takes the first match, so contracts that state the effective date outside that window or via unanticipated phrasing are missed; this is a documented precision-over-recall tradeoff, not a bug. Parties is the weakest category by a wide margin, for the annotation-inconsistency reason described in Bug 1 — this is the strongest real-world case for why the architecture routes non-standard extraction to the LLM path rather than trying to make regex handle it.

## Real eval numbers (LLM extractor, live API, 10-contract CUAD test-set sample, 2026-08-11, after bug #4 fix)

`python -m src.eval.run_eval --extractor llm --limit 10`, `extract_contract_per_category` (default mode), model `claude-sonnet-5`. 10 contracts is a small sample (compare to the rule-based table's full 102-contract run) — good enough to catch bugs #4/#5 above, not a stable enough number to treat any single category's F1 as a precise readout.

```
category                                 tp   fp   fn      P      R     F1
--------------------------------------------------------------------------
Affiliate License-Licensee                1   17    0   0.06   1.00   0.11
Affiliate License-Licensor                0   21    0   0.00    n/a    n/a
Agreement Date                            0    6    9   0.00   0.00   0.00
Anti-Assignment                           8    6    3   0.57   0.73   0.64
Audit Rights                              6    7    6   0.46   0.50   0.48
Cap On Liability                         10   14    8   0.42   0.56   0.48
Change Of Control                         2   12    4   0.14   0.33   0.20
Competitive Restriction Exception         4    9    0   0.31   1.00   0.47
Covenant Not To Sue                       2    0    1   1.00   0.67   0.80
Document Name                             0    2   10   0.00   0.00   0.00
Effective Date                            4    9    6   0.31   0.40   0.35
Exclusivity                               8   22    0   0.27   1.00   0.42
Expiration Date                           6    3    1   0.67   0.86   0.75
Governing Law                             9    0    0   1.00   1.00   1.00
Insurance                                 9    6    5   0.60   0.64   0.62
Ip Ownership Assignment                   6   15    7   0.29   0.46   0.35
Irrevocable Or Perpetual License          3    3    0   0.50   1.00   0.67
Joint Ip Ownership                        0    9    1   0.00   0.00   0.00
License Grant                            10   12   11   0.45   0.48   0.47
Liquidated Damages                        2    9    0   0.18   1.00   0.31
Minimum Commitment                        3   11    3   0.21   0.50   0.30
Most Favored Nation                       0    2    0   0.00    n/a    n/a
No-Solicit Of Customers                   0    1    0   0.00    n/a    n/a
No-Solicit Of Employees                   1    1    0   0.50   1.00   0.67
Non-Compete                               5   14    1   0.26   0.83   0.40
Non-Disparagement                         0    0    0    n/a    n/a    n/a
Non-Transferable License                  0   20    4   0.00   0.00   0.00
Notice Period To Terminate Renewal        1    1    2   0.50   0.33   0.40
Parties                                   2   18   41   0.10   0.05   0.06
Post-Termination Services                 6   41   12   0.13   0.33   0.18
Price Restrictions                        0    5    0   0.00    n/a    n/a
Renewal Term                              2    4    1   0.33   0.67   0.44
Revenue/Profit Sharing                    3   18    2   0.14   0.60   0.23
Rofr/Rofo/Rofn                            0    0    0    n/a    n/a    n/a
Source Code Escrow                        0    0    0    n/a    n/a    n/a
Termination For Convenience               3    3    4   0.50   0.43   0.46
Third Party Beneficiary                   0   17    0   0.00    n/a    n/a
Uncapped Liability                        3   22    1   0.12   0.75   0.21
Unlimited/All-You-Can-Eat-License         0    2    0   0.00    n/a    n/a
Volume Restriction                        0   18    0   0.00    n/a    n/a
Warranty Duration                         1    4    0   0.20   1.00   0.33
--------------------------------------------------------------------------
macro-avg F1 across 31 scored categories: 0.381
```

Full per-case report (every prediction vs. every ground-truth span, for manual spot-checking): `eval_reports/eval_llm_10_live.json`.

**Head-to-head on the 4 categories both extractors cover, same 10-contract sample** (rule-based numbers re-run at `--limit 10` for a fair comparison, not the 102-contract table above):

| category | rule P/R/F1 | LLM P/R/F1 |
|---|---|---|
| Agreement Date | 0.60 / 0.33 / 0.43 | 0.00 / 0.00 / 0.00 |
| Effective Date | n/a / 0.00 / n/a | 0.31 / 0.40 / 0.35 |
| Governing Law | 0.83 / 0.56 / 0.67 | **1.00 / 1.00 / 1.00** |
| Parties | n/a / 0.00 / n/a | 0.10 / 0.05 / 0.06 |

LLM strictly beats rule-based on Effective Date and Governing Law here, and gets *some* Parties recall where rule-based gets none — consistent with build-prompt.md's expectation that the LLM path should outperform regex on the categories regex can't reliably reach. Agreement Date is the outlier: LLM scored **0.00**, worse than rule-based's 0.43, for the reason documented in bug #5 above — Claude's citations for this category are over-broad (quoting the whole document-title-plus-opening-sentence rather than the date value alone), and the true date is buried inside every single one of those predictions on manual inspection, just not extracted as a clean span. This is the same root cause as the Document Name failures (0.00, 10/10 recall miss) above, not an independent issue.

## Cost-optimized eval attempt (2026-08-14): stratified sample + Batch API — real cost came in ~4x over the optimistic estimate

The few-shot prompt (bug #5, resolution attempt 2) was made the standing default in `extract_contract_per_category` — no code change needed there, it already was. Three things were then attempted for a cheaper, more representative full eval: (1) confirm prompt caching, (2) use the Message Batches API, (3) a stratified contract sample instead of `--limit N`'s first-N-alphabetically. (1) and (3) worked as intended; (2) did not deliver the savings it was supposed to, and that's the important finding here.

**(1) Prompt caching — confirmed live, with a caveat.** A calibration probe (`_document_block`'s `cache_control`) showed call 1 for a contract writing tokens to cache and calls 2-5 reading them back at ~0.1× price, exactly as designed. But the same calibration surfaced a real cost driver not previously accounted for: the ~567-token per-category instruction (the few-shot examples added in bug #5's fix) sits in a *separate*, uncached content block after the document — it's paid at full price on **every one of the 41 calls**, not just the first.

**(3) Stratified sample — 18 contracts, near-total category coverage.** Built via greedy set-cover over all 102 test contracts (rarest categories weighted first) rather than taking the first N. Result: all 40 CUAD categories that appear anywhere in the test set are covered by at least one contract in the sample — a real improvement over every prior `--limit N` run this session, where roughly half the categories scored `n/a` purely because the alphabetically-first contracts never happened to contain them. The 41st category, **"Price Restrictions," is absent from all 102 test contracts** — no sample, however chosen, can ever cover it; that's a corpus fact, not a sampling gap. Sample persisted at `eval_reports/stratified_sample_18.json`; selection code was a one-off (not kept in the repo — the sample itself is what's durable).

**(2) Batch API — the pre-run estimate assumed a mitigation that didn't fully work.** Pre-run analysis flagged a real risk: the Batches API gives no ordering guarantee, so submitting a contract's 41 per-category calls as one batch risks all of them racing to write the same cache entry with none reading it — losing caching's ~90% savings would cost far more than the 50% batch discount gives back. The planned mitigation (`src/extract/llm_extract_batch.py`): one synchronous warm-up call per contract first (1-hour cache TTL, to survive an unpredictable batch queue), *then* the other 40 calls per contract submitted through the Batch API, expected to read the now-warm cache.

Pre-run cost table (18-contract sample, `count_tokens`-based document estimate, intro pricing `$2`/`$10` per MTok):

| | Total | Risk |
|---|---|---|
| A. Sync + caching only (no Batch) | $8.83 | None — known-good |
| D. Pure Batch, caching lost to parallelism | $27.88 | Unmitigated |
| C. Pure Batch, caching somehow preserved | $4.42 | Optimistic, unverified |
| **E. Hybrid: warm-up + Batch (chosen)** | **$6.25** | Expected low |

**What actually happened:** the batch finished fast (395s for 720 requests, well under the "up to 1 hour" expectation) with zero request errors. But pulling real `usage` off the batch results showed only **74.6% of the 720 batched requests hit the cache (537/720); 25.4% missed and paid to write a fresh cache entry instead of reading the warm one.** Because a cache *write* costs 20x what a cache *read* costs (2.0x base price at 1-hour TTL vs. 0.1x), that 25% miss rate — not the 75% hit rate — dominated the bill: $17.70 of the batch's $21.88 total came from cache-write cost on the missed requests alone.

**Root cause, with real evidence:** miss rate correlates cleanly with *when in the sequential warm-up phase* a contract's cache entry was written, not randomly:

```
contract warmed up 1st, 6th, 11th, 16th (early in the ~61s warm-up loop): 2.5% miss rate
contract warmed up last, right before batch submission:                   47.5-67.5% miss rate
```

The synchronous warm-up call returning a response does not mean the cache entry is immediately visible to whatever distributed infrastructure the Batch API dispatches requests to — there's real propagation lag, on the order of at least the ~60 seconds this run's warm-up phase took, and contracts warmed up right before batch submission hadn't had time to propagate before batch requests for them started executing. This is a genuine finding about the platform, not a bug in this codebase's code (the warm-up call and every batched request use byte-identical document content and `cache_control`, verified as part of writing `llm_extract_batch.py`).

**Real total cost: ~$25.16** ($21.88 measured from the batch's actual `usage` fields + ~$3.28 estimated for the 18 synchronous warm-up calls) — **4x the $6.25 hybrid estimate, and close to the $27.88 worst-case bound**, i.e. the mitigation captured only a fraction of its intended benefit. It is also **worse than just not using the Batch API at all** (scenario A, $8.83, sync-only with ordinary 5-minute caching — which this session's earlier live calibration confirmed hits cache reliably on every call after the first, because there's no parallelism to race against).

Secondary finding, smaller effect but worth correcting for next time: real document tokens measured from the batch's `cache_creation_input_tokens` field ran **~23% higher** than what `messages.count_tokens` on the same plain text estimated (807,099 real vs. 656,402 estimated across the sample) — a citations-enabled `document` content block apparently carries real tokenization overhead beyond the raw text that `count_tokens` on a plain string doesn't capture. The pre-run cost estimates above are all understated by roughly this factor on the document-token line, independent of the caching-miss issue.

**Recommendation: don't use the Batch API for this per-category call pattern as currently implemented.** The synchronous path (`extract_contract_per_category`, scenario A) is simpler, already validated across every eval run this session, and empirically cheaper than the batch attempt turned out to be. `src/extract/llm_extract_batch.py` is left in the repo, working code (0 request errors, correct results, tests passing) — the code isn't broken, the cost model it was built to hit just doesn't hold up in practice. If Batch API cost savings are revisited: the concrete next thing to try, based on the propagation-lag evidence above, is inserting a deliberate buffer delay (e.g. 2-3 minutes) between the last warm-up call and batch submission, rather than submitting immediately after the warm-up loop finishes.

**Real eval numbers, 18-contract stratified sample, few-shot prompt, hybrid warm-up + Batch API extraction** (`eval_reports/eval_llm_batch_18.json`):

```
tp=389  fp=855  fn=499   P=0.313  R=0.438   macro-F1 across 40 scored categories: 0.398
```

Comparable precision/recall to the 10-contract alphabetical sample's Attempt-2 numbers (P=0.297, R=0.414, F1=0.404) but scored across 40 categories instead of a much smaller effectively-scored subset — this is the more trustworthy reading of the LLM extractor's real performance precisely because the sample was built not to leave categories at `n/a` by accident. Per-category breakdown is in the JSON report; the pattern from bug #5 (broad false-positive rate on categories the contract doesn't address, strongest on `Post-Termination Services` and `Exclusivity`) reproduces at this larger, more representative scale, consistent with Bug #5 being a real, still-open limitation rather than an artifact of the smaller 10-contract sample.

## Data note

CUAD's own repo already ships a train/test split as `train_separate_questions.json` / `test.json` inside `data.zip` (SQuAD 2.0 format) — the loader uses that split as-is rather than deriving a new one, per build-prompt.md. All 2,643 ground-truth spans in the test split were spot-checked (`src/data/cuad_loader.py` smoke test) to be exact substrings of their contract's text at the annotated offset — i.e. CUAD's own ground truth passes the same exactness bar the citation validator enforces on this system's output.

The upstream repo has moved since the dataset was published — `TheAtticusProject/cuad` now redirects to `The-Atticus-Project/cuad`. `scripts/fetch_cuad.py` resolves the repo via the GitHub API at run time (following the redirect) rather than hardcoding either name, per build-prompt.md's instruction not to guess a direct download URL.

## v1 close-out (2026-08-14)

### Confirmed architecture

- **Two extractors, split by category stability.** `src/extract/rules.py` (regex) targets the 4 categories with low-variance phrasing (Parties, Agreement Date, Effective Date, Governing Law); `src/extract/llm_extract.py` (Anthropic Citations API, `extract_contract_per_category`) covers the other 37 — obligations, liability caps, non-standard clauses — per build-prompt.md's scope split. Neither extractor was redesigned during the build; both were debugged in place against real eval runs (see bug table below).
- **The citation-validation layer is the actual point of v1**, not the extractors. `src/validate/citation_check.py` mechanically re-checks every `extracted_text` as an exact substring of the source document at its claimed offset, independent of which extractor produced it. Anything that fails is `unverified` and is structurally excluded from ever becoming a finding — enforced in `src/review/queue.py:approve`, which raises unconditionally for `reason == "unverified"` regardless of caller. This guarantee was exercised on every real eval run this session (thousands of live-API extractions across rule-based and LLM runs) and never found broken.
- **Confidence-gated review queue.** Verified extractions at or above `CONFIDENCE_THRESHOLD` (0.75, `src/config.py`) pass through as findings directly; verified extractions below it, and everything unverified, route to `src/review/queue.py` for human review rather than auto-approving. This is load-bearing for Bug 5 below — most of that bug's false positives never reach a user unreviewed.
- **Standing LLM prompt is bug #5's resolution attempt 2**: per-category Citations API calls with an explicit `NOT_FOUND` abstention instruction plus 3 few-shot examples. Kept as the best-available version despite being a documented, open precision/recall tradeoff, not a clean fix — see Bug 5.
- **Two extraction call patterns exist**, one recommended, one not: the default synchronous per-category loop with ephemeral prompt caching (`extract_contract_per_category`, validated cheap and reliable across every run this session) and an experimental hybrid warm-up + Batch API path (`src/extract/llm_extract_batch.py`, working code, zero request errors, but empirically *more* expensive than the default path in the one real run made against it — see the Batch API finding below). The synchronous path is the one to use; the batch path is documented infrastructure for a future revisit, not a recommendation.
- **Non-goals held throughout the build**, unchanged from the original scope: no RAG/case-law retrieval, no generative redlining or drafting, no playbook/ContractNLI-style entailment comparison. All deferred to v1.5/v2.

### Final eval numbers

- **Rule-based, full 102-contract test set** (the complete, stable measurement — `src/extract/rules.py` only targets these 4 categories): macro F1 **0.300**. Governing Law is the strongest category (P=0.98, R=0.63, F1=0.77 — "laws of the State of X" is low-variance). Parties is the weakest (F1=0.02) for a documented, non-bug reason: CUAD's own annotation is inconsistent about what counts as a "Parties" span (Bug 1's residual limitation).
- **LLM extractor, most representative run** — 18-contract stratified sample (chosen by greedy set-cover to reach 40/41 possible CUAD categories, vs. the `n/a`-heavy alphabetical-first-N samples used earlier in the session), standing few-shot prompt, hybrid warm-up + Batch API extraction: **tp=389, fp=855, fn=499 — P=0.313, R=0.438, macro F1=0.398** across 40 scored categories. This is the number to cite for "how good is the LLM extractor," not the earlier 10-contract runs, which under-covered categories badly enough to leave many at `n/a` by sampling accident rather than by measurement.
- **Head-to-head on the 4 categories both extractors cover** (10-contract sample): LLM beats rule-based on Effective Date (F1 0.35 vs. rule-based's unscored 0.00-recall) and Governing Law (F1 1.00 vs. 0.67); rule-based beats LLM on Agreement Date (F1 0.43 vs. LLM's 0.00, the value-style over-quoting problem shared with the Document Name failures); both are weak on Parties, for the same CUAD-annotation-inconsistency reason as the full rule-based run.
- **Overall v1 reading:** the citation-validation guarantee is solid and was the one architectural claim tested most heavily this session without exception. Rule-based extraction is precise-but-narrow by design on its 4 categories, a documented tradeoff rather than a defect. LLM extraction is the only path for the remaining 37 categories and carries a real, acknowledged precision ceiling (Bug 5, below) — substantially, but not completely, mitigated by the confidence-routing architecture rather than by the extraction quality itself.

### All bugs found, fixed and open — every one from a real eval run against real data, none found by inspection alone

| # | Bug | Found in | Status |
|---|---|---|---|
| 1 | `PARTIES_PREAMBLE_RE` swallowed sentence boundaries (greedy token class let comma/period terminate mid-token) | First rule-based eval run, 102-contract test set | **Fixed** |
| 2 | `GOVERNING_LAW_RE` jurisdiction capture truncated on internal apostrophes and lowercase connector words ("People's Republic of China" → "People") | Same run | **Fixed** |
| 3 | Eval harness's IoU-only match metric scored visibly-correct rule-based extractions (e.g. "New York") as 0, because CUAD's ground-truth spans are full sentences | Same run, after fixing bugs 1–2 | **Fixed** — eval methodology, not an extraction defect (see Bug 3's write-up on why this is *not* the same shape as the batch-caching / value-style-quoting gap, which was deliberately left unfixed for the opposite reason) |
| 4 | `extract_contract_per_category` crashed (`AttributeError`) on every real API response carrying a citation, because `_citations_to_clauses` assumed dict-shaped citations but the per-category path hands it live SDK objects | First live-API LLM run (`--extractor llm --limit 1`) — crashed on the very first citation returned | **Fixed** |
| 5 | LLM extractor has systematically low precision on categories a contract doesn't address — quotes topically-adjacent-but-wrong text (e.g. a rights-*disclaiming* clause as an answer to a rights-*granting* question) instead of abstaining | First 10-contract live LLM run | **Closed for v1 as a known limitation, not fixed.** Two resolution attempts: (1) bare `NOT_FOUND` instruction — net regression, recall fell more than precision improved; (2) few-shot examples added on top — precision up ~25% relative (0.238→0.297) and macro-F1 up, but recall still down ~9% relative (0.456→0.414) vs. the original baseline, so it does not clearly beat baseline on every axis. Per the explicit stopping rule applied when this was investigated, iteration stopped there rather than chasing a third prompt variant. Confidence-based review-queue routing means ~79% of the remaining false positives (the 0.6-confidence, multi-citation ones) never reach a user unreviewed — the real exposure is smaller than the raw false-positive count suggests, but the underlying precision gap is real and open. |
| — | Batch API cost-optimization attempt: a synchronous cache-warm-up call per contract, intended to guarantee cache hits before the remaining calls went through the 50%-off Message Batches API, only achieved a **74.6% real cache-hit rate** (537/720 requests) — not the near-100% the mitigation was designed for | 18-contract stratified cost-optimized eval | **Negative result, documented, not a code defect.** Root cause: real propagation lag between a synchronous cache write and the Batch API's distributed dispatch — miss rate correlated directly with how recently a contract's warm-up call had run (2.5% miss rate for the earliest-warmed contracts vs. 47.5–67.5% for the latest). Because a cache *write* costs 20x a cache *read*, that partial miss rate drove the real cost to ~$25.16 — 4x the $6.25 pre-run estimate, and worse than never using the Batch API at all ($8.83, the plain synchronous path). `src/extract/llm_extract_batch.py` is left in the repo as working, tested code; it is not the recommended extraction path. If revisited, the concrete next fix suggested by the evidence is a deliberate buffer delay (2-3 min) between the last warm-up call and batch submission. |

### Permanent non-determinism caveat

`claude-sonnet-5` — and the whole Claude 5 / 4.6+ model family the project is built on — rejects `temperature`, `top_p`, and `top_k` outright (400: "temperature is deprecated for this model"). There is no supported way to pin sampling determinism on this model generation; the parameters have to be omitted entirely, not set to a specific value (including `0`). This means **every LLM-extractor comparison in this document is a single run at the API's non-deterministic default, not a controlled, repeatable A/B** — the bug #5 baseline vs. resolution attempt 1 vs. attempt 2 numbers, and the sync-vs-batch cost comparison, all carry an irreducible amount of run-to-run sampling noise on top of whatever the code or prompt change actually did. This is flagged wherever it's directly relevant above, and is restated here as a standing property of the model this project runs on, not a gap that a future session can close by trying harder — any future re-measurement on this model family inherits the same caveat.
