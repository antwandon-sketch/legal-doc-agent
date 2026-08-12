"""Shared extraction schema used by both the rule-based and LLM extractors,
and consumed by the citation validator / review queue / eval harness."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ExtractedClause(BaseModel):
    contract_id: str
    clause_type: str
    extracted_text: str
    source_char_start: int = Field(ge=0)
    source_char_end: int = Field(gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    method: Literal["rule", "llm"]

    # Filled in by src/validate/citation_check.py. Anything still None here
    # has not yet been through the validator and must not be treated as a
    # finding.
    validation_status: Literal["verified", "unverified", None] = None

    @model_validator(mode="after")
    def _check_span_order(self) -> "ExtractedClause":
        if self.source_char_end <= self.source_char_start:
            raise ValueError(
                f"source_char_end ({self.source_char_end}) must be > "
                f"source_char_start ({self.source_char_start})"
            )
        return self
