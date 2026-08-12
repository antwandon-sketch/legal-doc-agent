"""Confidence + review-queue routing.

Extractions that are verified (see src/validate/citation_check.py) and at
or above CONFIDENCE_THRESHOLD are treated as auto-findings -- the citation
validator already guarantees their text is real, and high confidence means
no extraction ambiguity to resolve. Everything else (low confidence, or
unverified) is written to the review queue rather than silently passed
through or silently dropped.

Unverified items are queued for audit visibility only: `approve()` refuses
them unconditionally, so the "an approved finding can never cite text
absent from the source" guarantee holds even if a caller or UI bug tries to
approve one anyway -- the enforcement point is here, not just in the UI.
"""
import json
from datetime import datetime, timezone

from src.config import CONFIDENCE_THRESHOLD, REVIEW_QUEUE_DIR
from src.extract.schema import ExtractedClause
from src.review.models import ReviewQueueItem, make_item_id

QUEUE_FILE = REVIEW_QUEUE_DIR / "queue.json"
CONTEXT_CHARS = 200


def _context(full_text: str, start: int, end: int) -> tuple[str, str]:
    before = full_text[max(0, start - CONTEXT_CHARS) : start]
    after = full_text[end : end + CONTEXT_CHARS]
    return before, after


def route(
    clauses: list[ExtractedClause],
    full_text: str,
    threshold: float = CONFIDENCE_THRESHOLD,
) -> tuple[list[ExtractedClause], list[ReviewQueueItem]]:
    """Split validated clauses into (auto_findings, queue_items).

    Requires every clause to already have gone through
    src/validate/citation_check.py (validation_status set) -- raises if not,
    since routing an unvalidated clause either way would defeat the point
    of the validator.
    """
    auto_findings: list[ExtractedClause] = []
    queue_items: list[ReviewQueueItem] = []

    for clause in clauses:
        if clause.validation_status is None:
            raise ValueError(
                f"clause {clause.clause_type!r} for {clause.contract_id!r} has not "
                "been through citation validation -- call validate_batch() first"
            )

        if clause.validation_status == "verified" and clause.confidence >= threshold:
            auto_findings.append(clause)
            continue

        reason = "unverified" if clause.validation_status == "unverified" else "low_confidence"
        before, after = _context(full_text, clause.source_char_start, clause.source_char_end)
        queue_items.append(
            ReviewQueueItem(
                item_id=make_item_id(clause),
                contract_id=clause.contract_id,
                clause_type=clause.clause_type,
                extracted_text=clause.extracted_text,
                source_char_start=clause.source_char_start,
                source_char_end=clause.source_char_end,
                confidence=clause.confidence,
                method=clause.method,
                validation_status=clause.validation_status,
                reason=reason,
                context_before=before,
                context_after=after,
            )
        )

    return auto_findings, queue_items


def load_queue() -> list[ReviewQueueItem]:
    if not QUEUE_FILE.exists():
        return []
    with open(QUEUE_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    return [ReviewQueueItem.model_validate(item) for item in raw]


def save_queue(items: list[ReviewQueueItem]) -> None:
    REVIEW_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump([item.model_dump() for item in items], f, indent=2)


def enqueue(new_items: list[ReviewQueueItem]) -> list[ReviewQueueItem]:
    """Append new items, skipping any item_id already present (idempotent
    across repeated pipeline runs on the same contracts)."""
    existing = load_queue()
    existing_ids = {item.item_id for item in existing}
    merged = existing + [item for item in new_items if item.item_id not in existing_ids]
    save_queue(merged)
    return merged


def get_pending() -> list[ReviewQueueItem]:
    return [item for item in load_queue() if item.status == "pending"]


def _set_status(item_id: str, status: str, note: str | None) -> ReviewQueueItem:
    items = load_queue()
    for i, item in enumerate(items):
        if item.item_id == item_id:
            if status == "approved" and item.reason == "unverified":
                raise ValueError(
                    f"item {item_id} is unverified (its cited text does not match the "
                    "source document at the claimed offset) and can never be approved "
                    "as a finding -- this is enforced regardless of caller"
                )
            updated = item.model_copy(
                update={
                    "status": status,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    "reviewer_note": note,
                }
            )
            items[i] = updated
            save_queue(items)
            return updated
    raise KeyError(f"no queue item with id {item_id}")


def approve(item_id: str, note: str | None = None) -> ReviewQueueItem:
    return _set_status(item_id, "approved", note)


def reject(item_id: str, note: str | None = None) -> ReviewQueueItem:
    return _set_status(item_id, "rejected", note)


def get_approved_findings() -> list[ExtractedClause]:
    """Queue items a human has approved, converted back into ExtractedClause
    so they can be combined with auto-findings downstream."""
    findings = []
    for item in load_queue():
        if item.status != "approved":
            continue
        findings.append(
            ExtractedClause(
                contract_id=item.contract_id,
                clause_type=item.clause_type,
                extracted_text=item.extracted_text,
                source_char_start=item.source_char_start,
                source_char_end=item.source_char_end,
                confidence=item.confidence,
                method=item.method,
                validation_status=item.validation_status,
            )
        )
    return findings
