#!/usr/bin/env python3
"""Cost-optimized eval run against a fixed, stratified contract sample, using
the hybrid warm-up + Message Batches API extractor (src/extract/
llm_extract_batch.py). See PROJECT.md's Batch API cost-tradeoff writeup for
why this exists instead of just `run_eval.py --extractor llm --limit N`:

- `--limit N` takes the first N contracts in file order, which skews hard
  toward whatever categories happen to be present in early contracts (see
  the "Real eval numbers (LLM extractor...)" 10-contract runs, most of which
  scored 0 tp/0 fp/0 fn -- n/a by construction -- on categories the sample
  never happened to include). This sample was chosen by greedy set-cover
  over the full 102-contract test set to cover every CUAD category that
  appears anywhere in it.
- The synchronous per-category loop pays full document-token price on every
  call except the first (mitigated there by ephemeral 5-minute cache reads).
  Here, one synchronous warm-up call per contract (1-hour TTL) is followed
  by the other 40 calls per contract through the Batch API at 50% off,
  reading the already-warm cache -- see llm_extract_batch's module docstring
  for why the warm-up has to happen synchronously first.

Usage:
    python -m src.eval.run_eval_batch --sample-file eval_reports/stratified_sample_18.json
"""
import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.config import EVAL_REPORTS_DIR  # noqa: E402
from src.data.cuad_loader import get_category_questions, load_test  # noqa: E402
from src.eval.run_eval import aggregate, print_report, score_contract  # noqa: E402
from src.extract import llm_extract_batch  # noqa: E402
from src.validate.citation_check import validate_batch  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-file", required=True, help="JSON list of {contract_id: ...} rows")
    parser.add_argument("--out", default=None)
    parser.add_argument("--poll-interval", type=float, default=30.0)
    args = parser.parse_args()

    sample = json.loads(Path(args.sample_file).read_text())
    sample_ids = [row["contract_id"] for row in sample]

    all_records = {r.contract_id: r for r in load_test()}
    records = [all_records[cid] for cid in sample_ids if cid in all_records]
    missing = [cid for cid in sample_ids if cid not in all_records]
    if missing:
        print(f"WARNING: {len(missing)} sample contract_ids not found in the test set: {missing}")

    categories = get_category_questions()
    print(f"Submitting hybrid extraction for {len(records)} contracts x {len(categories)} categories...")

    t0 = time.time()
    job = llm_extract_batch.submit_batch_extraction(records, categories)
    print(f"Warm-up calls done ({time.time() - t0:.0f}s), batch submitted: {job.batch_id}")
    print(f"{len(job.warmup_clauses)} clauses from warm-up calls; "
          f"{len(job.index_map)} requests in the batch. Waiting for the batch to complete "
          "(Anthropic: most batches finish within 1 hour, max 24h)...")

    t1 = time.time()
    clauses, errors = llm_extract_batch.wait_and_collect(job, poll_interval_seconds=args.poll_interval)
    print(f"Batch finished after {time.time() - t1:.0f}s. {len(clauses)} total clauses, {len(errors)} errors.")
    if errors:
        print("Errored requests (not silently dropped -- these contracts/categories have no prediction):")
        for e in errors[:20]:
            print("  -", e)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")

    by_contract: dict[str, list] = {}
    for c in clauses:
        by_contract.setdefault(c.contract_id, []).append(c)

    all_cases = []
    for record in records:
        preds = by_contract.get(record.contract_id, [])
        validated = validate_batch(preds, record.full_text)
        all_cases.extend(score_contract(record, validated))

    table = aggregate(all_cases)
    print()
    print_report(table)

    EVAL_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else EVAL_REPORTS_DIR / f"eval_llm_batch_{len(records)}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "extractor": "llm_batch_hybrid",
                "num_contracts": len(records),
                "batch_id": job.batch_id,
                "errors": errors,
                "aggregate": table,
                "cases": [asdict(c) for c in all_cases],
            },
            f,
            indent=2,
        )
    print(f"\nFull report (aggregate + per-case) written to {out_path}")


if __name__ == "__main__":
    main()
