#!/usr/bin/env python3
"""End-to-end pipeline: fetch (if needed) -> extract -> validate -> route to
auto-findings / review queue.

Usage:
    python scripts/run_pipeline.py --limit 25
    python scripts/run_pipeline.py --limit 10 --use-llm   # needs ANTHROPIC_API_KEY
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RAW_DIR  # noqa: E402
from src.data.cuad_loader import get_category_questions, load_test  # noqa: E402
from src.extract import rules  # noqa: E402
from src.extract.schema import ExtractedClause  # noqa: E402
from src.review import queue as queue_mod  # noqa: E402
from src.validate.citation_check import validate_batch  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--use-llm", action="store_true", help="also run LLM extraction (needs ANTHROPIC_API_KEY)")
    parser.add_argument("--llm-mode", choices=["per_category", "batched"], default="per_category")
    parser.add_argument("--out", type=str, default="findings.json")
    args = parser.parse_args()

    if not RAW_DIR.exists() or not any(RAW_DIR.iterdir()):
        print("CUAD corpus not found -- run scripts/fetch_cuad.py first.")
        sys.exit(1)

    categories = get_category_questions() if args.use_llm else None
    if args.use_llm:
        from src.extract import llm_extract  # deferred: needs ANTHROPIC_API_KEY at call time

    records = list(load_test())[: args.limit]
    print(f"Running pipeline on {len(records)} CUAD test contracts (use_llm={args.use_llm})")

    all_findings: list[ExtractedClause] = []
    all_queue_items = []
    total_extracted = 0

    for i, record in enumerate(records, 1):
        preds: list[ExtractedClause] = rules.extract_all(record.contract_id, record.full_text)

        if args.use_llm:
            if args.llm_mode == "per_category":
                preds.extend(
                    llm_extract.extract_contract_per_category(record.contract_id, record.full_text, categories)
                )
            else:
                preds.extend(
                    llm_extract.extract_contract_batched(record.contract_id, record.full_text, categories)
                )

        validated = validate_batch(preds, record.full_text)
        auto, queued = queue_mod.route(validated, record.full_text)

        total_extracted += len(validated)
        all_findings.extend(auto)
        all_queue_items.extend(queued)

        print(f"  [{i}/{len(records)}] {record.contract_id[:60]:60s} "
              f"extracted={len(validated):3d} auto={len(auto):3d} queued={len(queued):3d}")

    queue_mod.enqueue(all_queue_items)

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([c.model_dump() for c in all_findings], f, indent=2)

    print()
    print(f"Contracts processed:      {len(records)}")
    print(f"Total extractions:        {total_extracted}")
    print(f"Auto-approved findings:   {len(all_findings)}  (verified, confidence >= threshold)")
    print(f"Sent to review queue:     {len(all_queue_items)}")
    unverified = sum(1 for c in all_queue_items if c.reason == "unverified")
    print(f"  of which unverified:    {unverified}  (citation check failed -- can never be approved)")
    print(f"Findings written to:      {out_path}")
    print(f"Review queue file:        {queue_mod.QUEUE_FILE}")
    print(f"Run the review UI with:   python scripts/run_review_ui.py")


if __name__ == "__main__":
    main()
