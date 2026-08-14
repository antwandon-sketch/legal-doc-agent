#!/usr/bin/env python3
"""Adversarial demo, in two acts, of what happens when the LLM extractor is
asked about a clause category a contract genuinely does not contain.

This exercises the one guarantee v1 is actually built around (see
PROJECT.md): src/validate/citation_check.py mechanically re-checks every
extracted_text against the source document at its claimed offset, and
src/review/queue.py:approve refuses -- unconditionally, server-side -- to
ever let an unverified item become an approved finding.

Act 1 -- REAL model attempt. Tries the live LLM extractor against a CUAD
contract for several categories PROJECT.md's Bug #5 documents as
false-positive-prone (the model quotes real, topically-adjacent contract
text instead of abstaining with NOT_FOUND). This usually reproduces a real
false attribution: a genuine substring of the contract, at a correct
offset, just not evidence of the category asked about. The offset
validator correctly marks this `verified` -- it IS real text at that
offset -- and this is the honest limit of what it promises: it guarantees
"this text exists in the document at this offset," not "this text answers
the question asked." That gap is exactly Bug #5, and it's why this
category of false positive is caught by the confidence-based review-queue
routing instead (multi-citation confidence 0.6 < CONFIDENCE_THRESHOLD 0.75
-> queued, not auto-approved) -- not by the citation validator.

Act 2 -- DELIBERATELY CORRUPTED span. Takes one of the same real citations
and shifts its offset by one character, so extracted_text is no longer an
exact substring of the source at that claimed location -- the actual shape
of hallucination the validator exists to catch (see its module docstring:
"chunking edge cases, off-by-one offsets, whitespace normalization
differences"). Labeled SIMULATED throughout -- it is not a claim about
what the model said, only a demonstration of what happens when *something*
in the pipeline claims it.

Usage:
    python demos/adversarial_citation_demo.py
    python demos/adversarial_citation_demo.py --no-live   # skip the API call, go straight to Act 2
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.cuad_loader import ContractRecord, get_category_questions, load_test  # noqa: E402
from src.extract.schema import ExtractedClause  # noqa: E402
from src.review import queue as queue_mod  # noqa: E402
from src.validate.citation_check import validate_batch  # noqa: E402

# Ordered by descending false-positive count in PROJECT.md's Bug #5 table
# (10-contract live run) -- most FP-prone categories first, to maximize the
# chance a single live pass reproduces a real false attribution.
CANDIDATE_CATEGORIES = [
    "Post-Termination Services",
    "Uncapped Liability",
    "Non-Transferable License",
    "Volume Restriction",
    "Third Party Beneficiary",
    "Revenue/Profit Sharing",
    "Affiliate License-Licensee",
]

CONTRACT_ID_PREFIX = "LohaCompanyltd"  # 35/41 categories absent -- covers every candidate above


def pick_contract() -> ContractRecord:
    for record in load_test():
        if record.contract_id.startswith(CONTRACT_ID_PREFIX):
            return record
    raise RuntimeError(f"no test contract found with id prefix {CONTRACT_ID_PREFIX!r}")


def try_live_attempt(record: ContractRecord, categories: list[str]) -> tuple[str, list[ExtractedClause]]:
    """Try each candidate category in order against the live extractor until
    one returns a citation. Returns (category, claims); claims is empty if
    every candidate was correctly abstained on."""
    from src.extract import llm_extract  # deferred: needs ANTHROPIC_API_KEY at call time

    questions = get_category_questions()
    for category in categories:
        if category not in record.absent_categories:
            continue
        print(f"  trying {category!r}...", end=" ")
        claims = llm_extract.extract_contract_per_category(
            record.contract_id, record.full_text, {category: questions[category]}
        )
        if claims:
            print(f"got {len(claims)} citation(s)")
            return category, claims
        print("abstained (NOT_FOUND)")
    return categories[0], []


def build_corrupted_claim(base: ExtractedClause) -> ExtractedClause:
    """Same clause_type and confidence as `base`, but the offset is shifted
    by one character -- the exact shape tests/test_citation_check.py's
    offset-shift case covers, narrated here as an adversarial claim."""
    return base.model_copy(
        update={
            "source_char_start": base.source_char_start + 1,
            "source_char_end": base.source_char_end + 1,
        }
    )


def run_validator_and_queue(claim: ExtractedClause, source: str):
    validated = validate_batch([claim], source)[0]
    auto, queued = queue_mod.route([validated], source)
    queue_mod.enqueue(queued)  # persist so approve()/reject() can look items up by id
    return validated, auto, queued


def print_claim_vs_reality(label: str, claim: ExtractedClause, source: str):
    actual_at_offset = source[claim.source_char_start : claim.source_char_end]
    print(f"{label}")
    print(f"  clause_type:        {claim.clause_type}")
    print(f"  extracted_text:     {claim.extracted_text!r}")
    print(f"  source_char_start:  {claim.source_char_start}")
    print(f"  source_char_end:    {claim.source_char_end}")
    print(f"  confidence:         {claim.confidence}")
    print(f"  text ACTUALLY at that offset: {actual_at_offset!r}")


def print_validator_and_queue_result(validated: ExtractedClause, auto, queued):
    print(f"  validation_status:  {validated.validation_status}")
    print(f"  auto-approved:      {len(auto) == 1}")
    if queued:
        item = queued[0]
        print(f"  routed to queue:    reason={item.reason!r} status={item.status!r}")
        if item.reason == "unverified":
            print("  attempting queue_mod.approve() anyway "
                  "(simulating a reviewer/caller trying to wave it through):")
            try:
                queue_mod.approve(item.item_id)
                print("    !! approved -- this would be a bug in queue.py, did not happen.")
            except ValueError as e:
                print(f"    approve() raised ValueError: {e}")
        else:
            print("  (verified + low-confidence -> a human reviewer CAN approve this one; "
                  "that's by design, see PROJECT.md's confidence-routing note. The category "
                  "attribution is still wrong -- a reviewer, not the validator, has to catch that.)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-live", action="store_true", help="skip Act 1's live API call")
    args = parser.parse_args()

    record = pick_contract()

    print("=" * 78)
    print("ADVERSARIAL CITATION DEMO")
    print("=" * 78)
    print(f"Contract: {record.contract_id}")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        # Isolate the demo's queue writes from the repo's real review_queue/queue.json.
        queue_mod.QUEUE_FILE = Path(tmp) / "queue.json"

        # ---- Act 1: real model attempt -----------------------------------
        print("ACT 1 -- real live LLM extractor, categories CUAD marks absent from this contract")
        print("-" * 78)
        category, claims = ("", [])
        if not args.no_live:
            try:
                category, claims = try_live_attempt(record, CANDIDATE_CATEGORIES)
            except Exception as e:  # e.g. no ANTHROPIC_API_KEY
                print(f"  live attempt unavailable: {e}")
        else:
            print("  (skipped: --no-live)")
        print()

        if claims:
            print(f"Model was asked about {category!r} -- CUAD ground truth says this contract")
            print("does not contain that clause. It answered anyway:")
            print()
            for claim in claims:
                validated, auto, queued = run_validator_and_queue(claim, record.full_text)
                print_claim_vs_reality("BEFORE (model's claim):", claim, record.full_text)
                print()
                print("AFTER (validator + queue):")
                print_validator_and_queue_result(validated, auto, queued)
                print()
            base_for_act2 = claims[0]
        else:
            print("Every candidate category was correctly abstained on (NOT_FOUND, no")
            print("citation) -- a good outcome for the extractor, but nothing here for Act 1")
            print("to show. Falling through to Act 2 using a synthetic base clause.")
            print()
            category = CANDIDATE_CATEGORIES[0]
            anchor = "Contract"
            idx = record.full_text.index(anchor)
            base_for_act2 = ExtractedClause(
                contract_id=record.contract_id,
                clause_type=category,
                extracted_text=record.full_text[idx : idx + len(anchor)],
                source_char_start=idx,
                source_char_end=idx + len(anchor),
                confidence=0.85,
                method="llm",
            )

        # ---- Act 2: deliberately corrupted span ---------------------------
        print("=" * 78)
        print("ACT 2 -- deliberately corrupted span (SIMULATED, not a real model claim)")
        print("-" * 78)
        print("Same category and same real contract text as above, but the claimed offset")
        print("is shifted by one character -- the actual shape of hallucination the")
        print("citation validator exists to catch (off-by-one / chunking-edge-case span).")
        print()
        corrupted = build_corrupted_claim(base_for_act2)
        validated, auto, queued = run_validator_and_queue(corrupted, record.full_text)
        print_claim_vs_reality("BEFORE (simulated claim):", corrupted, record.full_text)
        print()
        print("AFTER (validator + queue):")
        print_validator_and_queue_result(validated, auto, queued)
        print()
        print("=" * 78)
        print("SUMMARY")
        print("=" * 78)
        print("Act 1 showed the validator's honest boundary: it verifies text-at-offset,")
        print("not category-correctness, so a real-but-wrongly-attributed citation reads")
        print("as 'verified' and is instead caught by confidence-based review routing.")
        print("Act 2 showed the validator's actual job: any claim whose text does not")
        print("exactly match the source at its claimed offset is marked 'unverified' and")
        print("is structurally, server-side blocked from ever becoming an approved")
        print("finding -- enforced in src/review/queue.py:approve regardless of caller.")


if __name__ == "__main__":
    main()
