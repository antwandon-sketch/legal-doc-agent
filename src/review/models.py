import hashlib
from typing import Literal, Optional

from pydantic import BaseModel

from src.extract.schema import ExtractedClause


class ReviewQueueItem(BaseModel):
    item_id: str
    contract_id: str
    clause_type: str
    extracted_text: str
    source_char_start: int
    source_char_end: int
    confidence: float
    method: Literal["rule", "llm"]
    validation_status: Literal["verified", "unverified"]
    # Why this landed in the queue. "unverified" items are shown for audit
    # visibility only and can never be approved as a finding -- see
    # src/review/queue.py:approve, which enforces this server-side, not
    # just in the UI.
    reason: Literal["unverified", "low_confidence"]
    context_before: str
    context_after: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    reviewed_at: Optional[str] = None
    reviewer_note: Optional[str] = None


def make_item_id(clause: ExtractedClause) -> str:
    key = f"{clause.contract_id}|{clause.clause_type}|{clause.source_char_start}|{clause.source_char_end}|{clause.method}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]
