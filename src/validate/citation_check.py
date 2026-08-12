"""Deterministic citation validator -- the non-negotiable core of v1.

Independent of the extraction step (rule-based or LLM), even though the
Citations API already returns character offsets: this re-checks that
`extracted_text` is an *exact* substring of the source document at the
claimed offset. Citations narrows hallucination risk but doesn't eliminate
it (chunking edge cases, off-by-one offsets, whitespace normalization
differences between what the model echoes and the raw source text), so
this check is not skipped or merged into the extraction step.

Anything that fails is marked `validation_status: unverified` and must be
excluded from anything the system presents as a finding. No fuzzy
matching, no "close enough" -- an offset mismatch of even one character
fails the check.
"""
from src.extract.schema import ExtractedClause


def validate_clause(clause: ExtractedClause, full_text: str) -> ExtractedClause:
    """Returns a copy of `clause` with validation_status set. Never mutates
    extracted_text/offsets -- a failed check means the clause is unverified,
    not "corrected"."""
    actual = full_text[clause.source_char_start : clause.source_char_end]
    status = "verified" if actual == clause.extracted_text else "unverified"
    return clause.model_copy(update={"validation_status": status})


def validate_batch(
    clauses: list[ExtractedClause], full_text: str
) -> list[ExtractedClause]:
    return [validate_clause(c, full_text) for c in clauses]


def only_verified(clauses: list[ExtractedClause]) -> list[ExtractedClause]:
    """The one function anything downstream that presents "findings" should
    call -- anything that hasn't passed validate_clause with status
    "verified" is filtered out here, structurally, rather than relying on
    every caller to remember to check validation_status themselves."""
    return [c for c in clauses if c.validation_status == "verified"]
