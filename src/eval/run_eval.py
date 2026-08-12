#!/usr/bin/env python3
"""Eval harness: per clause-category precision/recall/F1 against CUAD ground
truth, with a per-case report (expected span vs predicted span, side by
side) so a wrong case is inspectable, not just a statistic baked into one
aggregate number.

A predicted span counts as a true positive only if it is BOTH
validation_status == "verified" AND overlaps a ground-truth span for the
same (contract, category) at IoU >= OVERLAP_THRESHOLD. An unverified
prediction can never count as correct, no matter how close its offsets
are -- consistent with src/validate/citation_check.py's "no exceptions, no
fuzzy matching" rule for what counts as a finding.

Usage:
    python -m src.eval.run_eval --extractor rule --limit 50
    python -m src.eval.run_eval --extractor llm --limit 10   # needs ANTHROPIC_API_KEY
    python -m src.eval.run_eval --extractor both --limit 10
"""
import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.config import EVAL_REPORTS_DIR  # noqa: E402
from src.data.cuad_loader import ContractRecord, get_category_questions, load_test  # noqa: E402
from src.extract import rules  # noqa: E402
from src.extract.schema import ExtractedClause  # noqa: E402
from src.validate.citation_check import validate_batch  # noqa: E402

OVERLAP_THRESHOLD = 0.5


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    inter = max(0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return inter / union if union > 0 else 0.0


def _is_span_match(pred_start: int, pred_end: int, gt_start: int, gt_end: int) -> bool:
    """A predicted span matches a ground-truth span if either:

    - they overlap at IoU >= OVERLAP_THRESHOLD (the standard extractive-span
      metric), or
    - the predicted span is fully contained inside the ground-truth span.

    The containment clause exists because CUAD annotates full clause
    sentences (its task is "highlight the lawyer-reviewable text"), while
    src/extract/rules.py deliberately extracts just the value inside that
    clause (the date, the jurisdiction name, the entity name) -- which is
    more useful for downstream structured output but has near-zero IoU
    against a sentence-length ground-truth span even when it's the exact
    right value in the exact right place. See PROJECT.md bug log: this was
    found by an initial eval run scoring Governing Law/Parties at ~0 P/R
    despite spot-checked predictions being correct.
    """
    if _overlap(pred_start, pred_end, gt_start, gt_end) >= OVERLAP_THRESHOLD:
        return True
    return gt_start <= pred_start and pred_end <= gt_end


@dataclass
class CaseResult:
    contract_id: str
    clause_type: str
    ground_truth: list[str]
    predicted: list[dict]
    tp: int
    fp: int
    fn: int


def score_contract(record: ContractRecord, predictions: list[ExtractedClause]) -> list[CaseResult]:
    gt_by_cat: dict[str, list] = {}
    for span in record.ground_truth_spans:
        gt_by_cat.setdefault(span.clause_type, []).append(span)
    for cat in record.absent_categories:
        gt_by_cat.setdefault(cat, [])

    pred_by_cat: dict[str, list[ExtractedClause]] = {}
    for p in predictions:
        pred_by_cat.setdefault(p.clause_type, []).append(p)

    categories = set(gt_by_cat) | set(pred_by_cat)
    results = []
    for cat in sorted(categories):
        gts = gt_by_cat.get(cat, [])
        preds = pred_by_cat.get(cat, [])
        matched_gt: set[int] = set()
        pred_records = []
        tp = 0

        for p in preds:
            best_j, best_overlap = None, 0.0
            if p.validation_status == "verified":
                for j, gt in enumerate(gts):
                    if j in matched_gt:
                        continue
                    ov = _overlap(p.source_char_start, p.source_char_end, gt.char_start, gt.char_end)
                    if _is_span_match(p.source_char_start, p.source_char_end, gt.char_start, gt.char_end) and ov >= best_overlap:
                        best_overlap, best_j = ov, j
            is_match = p.validation_status == "verified" and best_j is not None
            if is_match:
                matched_gt.add(best_j)
                tp += 1
            pred_records.append(
                {
                    "text": p.extracted_text,
                    "validation_status": p.validation_status,
                    "confidence": p.confidence,
                    "method": p.method,
                    "matched": is_match,
                }
            )

        fp = len(preds) - tp
        fn = len(gts) - len(matched_gt)
        results.append(
            CaseResult(
                contract_id=record.contract_id,
                clause_type=cat,
                ground_truth=[g.text for g in gts],
                predicted=pred_records,
                tp=tp,
                fp=fp,
                fn=fn,
            )
        )
    return results


def aggregate(cases: list[CaseResult]) -> dict[str, dict]:
    per_cat: dict[str, dict] = {}
    for c in cases:
        agg = per_cat.setdefault(c.clause_type, {"tp": 0, "fp": 0, "fn": 0})
        agg["tp"] += c.tp
        agg["fp"] += c.fp
        agg["fn"] += c.fn

    table = {}
    for cat, agg in per_cat.items():
        tp, fp, fn = agg["tp"], agg["fp"], agg["fn"]
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        if precision is not None and recall is not None and (precision + recall) > 0:
            f1 = 2 * precision * recall / (precision + recall)
        elif precision is not None and recall is not None:
            f1 = 0.0
        else:
            f1 = None
        table[cat] = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}
    return table


def print_report(table: dict[str, dict]) -> None:
    def fmt(x):
        return f"{x:.2f}" if x is not None else "n/a"

    header = f"{'category':38s} {'tp':>4s} {'fp':>4s} {'fn':>4s} {'P':>6s} {'R':>6s} {'F1':>6s}"
    print(header)
    print("-" * len(header))
    scored = []
    for cat in sorted(table):
        row = table[cat]
        print(
            f"{cat:38s} {row['tp']:4d} {row['fp']:4d} {row['fn']:4d} "
            f"{fmt(row['precision']):>6s} {fmt(row['recall']):>6s} {fmt(row['f1']):>6s}"
        )
        if row["f1"] is not None:
            scored.append(row["f1"])
    print("-" * len(header))
    macro_f1 = sum(scored) / len(scored) if scored else 0.0
    print(f"macro-avg F1 across {len(scored)} scored categories: {macro_f1:.3f}")
    print("(per-category breakdown above is the actual result; macro-avg is a summary, not the headline)")


def run_predictions(record: ContractRecord, extractor: str) -> list[ExtractedClause]:
    preds: list[ExtractedClause] = []
    if extractor in ("rule", "both"):
        preds.extend(rules.extract_all(record.contract_id, record.full_text))
    if extractor in ("llm", "both"):
        from src.extract import llm_extract  # deferred: only needed (and only importable-with-key) here

        categories = get_category_questions()
        preds.extend(
            llm_extract.extract_contract_per_category(record.contract_id, record.full_text, categories)
        )
    return preds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extractor", choices=["rule", "llm", "both"], default="rule")
    parser.add_argument("--limit", type=int, default=25, help="number of test contracts to evaluate")
    parser.add_argument("--out", type=str, default=None, help="output JSON report path")
    args = parser.parse_args()

    records = list(load_test())[: args.limit]
    print(f"Evaluating {len(records)} test contracts with extractor={args.extractor!r}")

    all_cases: list[CaseResult] = []
    for record in records:
        preds = run_predictions(record, args.extractor)
        validated = validate_batch(preds, record.full_text)
        all_cases.extend(score_contract(record, validated))

    table = aggregate(all_cases)
    print()
    print_report(table)

    EVAL_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else EVAL_REPORTS_DIR / f"eval_{args.extractor}_{len(records)}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "extractor": args.extractor,
                "num_contracts": len(records),
                "overlap_threshold": OVERLAP_THRESHOLD,
                "aggregate": table,
                "cases": [asdict(c) for c in all_cases],
            },
            f,
            indent=2,
        )
    print(f"\nFull report (aggregate + per-case) written to {out_path}")


if __name__ == "__main__":
    main()
