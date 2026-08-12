"""The one property worth writing a test specifically for (per build-prompt.md
Definition of Done): an approved finding can never cite text that isn't
actually in the source document at the claimed offset.
"""
from src.extract.schema import ExtractedClause
from src.validate.citation_check import only_verified, validate_batch, validate_clause

SOURCE = (
    "This Supply Agreement is made between Acme Corp and Beta LLC. "
    "This Agreement shall be governed by the laws of the State of Delaware."
)


def _clause(text, start, end, clause_type="Governing Law"):
    return ExtractedClause(
        contract_id="test-contract",
        clause_type=clause_type,
        extracted_text=text,
        source_char_start=start,
        source_char_end=end,
        confidence=0.9,
        method="llm",
    )


def test_exact_match_is_verified():
    start = SOURCE.index("Delaware")
    clause = _clause("Delaware", start, start + len("Delaware"))

    result = validate_clause(clause, SOURCE)

    assert result.validation_status == "verified"


def test_hallucinated_text_at_real_offset_is_unverified():
    # Offsets point at real text ("Acme Corp") but the claimed quote doesn't
    # match it -- this is exactly the hallucination shape the validator
    # exists to catch: a model asserting a quote that isn't actually there.
    start = SOURCE.index("Acme Corp")
    clause = _clause("Gamma Industries", start, start + len("Gamma Industries"), clause_type="Parties")

    result = validate_clause(clause, SOURCE)

    assert result.validation_status == "unverified"


def test_offset_shifted_by_one_character_fails_no_fuzzy_matching():
    start = SOURCE.index("Delaware")
    clause = _clause("Delaware", start + 1, start + 1 + len("Delaware"))

    result = validate_clause(clause, SOURCE)

    assert result.validation_status == "unverified"


def test_offsets_pointing_outside_document_bounds_fail_safely():
    clause = _clause("nonexistent", len(SOURCE) + 50, len(SOURCE) + 61)

    result = validate_clause(clause, SOURCE)

    assert result.validation_status == "unverified"


def test_only_verified_excludes_every_unverified_clause():
    start = SOURCE.index("Delaware")
    real = _clause("Delaware", start, start + len("Delaware"))
    hallucinated = _clause("California", start, start + len("Delaware"), clause_type="Parties")

    validated = validate_batch([real, hallucinated], SOURCE)
    findings = only_verified(validated)

    assert len(findings) == 1
    assert findings[0].extracted_text == "Delaware"


def test_unvalidated_clause_is_never_treated_as_a_finding():
    # A clause that has never been through validate_clause at all
    # (validation_status is still None) must not slip into `only_verified`
    # just because nothing has actively flagged it as bad.
    start = SOURCE.index("Delaware")
    clause = _clause("Delaware", start, start + len("Delaware"))

    assert clause.validation_status is None
    assert only_verified([clause]) == []


def test_no_exceptions_carved_out_for_near_misses():
    # Claimed text is "Delaware" but the offset span is one character short
    # of covering it -- a near miss, not an exact match, so it must fail
    # just like a completely wrong offset would.
    real_start = SOURCE.index("Delaware")
    clause = _clause("Delaware", real_start, real_start + len("Delaware") - 1)

    result = validate_clause(clause, SOURCE)

    assert result.validation_status == "unverified"
