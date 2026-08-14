"""Unit tests for src/extract/llm_extract_batch.py's pure bookkeeping logic
(custom_id -> (contract_id, clause_type) attribution, partial-failure
handling) against synthetic batch results -- no live API calls, no batch
submission. Mirrors tests/test_llm_extract_parsing.py's style.
"""
from types import SimpleNamespace

from src.extract.llm_extract_batch import BatchExtractionJob, wait_and_collect
from src.extract.schema import ExtractedClause


def _citation(text, start, end):
    return SimpleNamespace(
        type="char_location", cited_text=text, start_char_index=start, end_char_index=end
    )


def _text_block(citations=None):
    return SimpleNamespace(type="text", text="", citations=citations)


class _FakeBatch:
    def __init__(self, results):
        self._results = results

    def retrieve(self, batch_id):
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id):
        return iter(self._results)


class _FakeMessages:
    def __init__(self, batch_results):
        self.batches = _FakeBatch(batch_results)


class _FakeClient:
    def __init__(self, batch_results):
        self.messages = _FakeMessages(batch_results)


def _succeeded(custom_id, citations):
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(content=[_text_block(citations)]),
        ),
    )


def _errored(custom_id):
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="errored"))


def test_attributes_result_to_contract_and_category_via_index_map():
    job = BatchExtractionJob(
        batch_id="batch_1",
        index_map={"c0q0": ("contract-a", "Governing Law")},
        warmup_clauses=[],
    )
    batch_results = [
        _succeeded("c0q0", [_citation("laws of Delaware", 10, 27)]),
    ]
    clauses, errors = wait_and_collect(job, client=_FakeClient(batch_results), poll_interval_seconds=0)

    assert errors == []
    assert len(clauses) == 1
    assert isinstance(clauses[0], ExtractedClause)
    assert clauses[0].contract_id == "contract-a"
    assert clauses[0].clause_type == "Governing Law"
    assert clauses[0].extracted_text == "laws of Delaware"


def test_warmup_clauses_are_included_alongside_batch_results():
    warmup = [
        ExtractedClause(
            contract_id="contract-a",
            clause_type="Document Name",
            extracted_text="SUPPLY CONTRACT",
            source_char_start=0,
            source_char_end=15,
            confidence=0.85,
            method="llm",
        )
    ]
    job = BatchExtractionJob(batch_id="batch_1", index_map={}, warmup_clauses=warmup)
    clauses, errors = wait_and_collect(job, client=_FakeClient([]), poll_interval_seconds=0)

    assert errors == []
    assert clauses == warmup


def test_errored_result_is_reported_not_silently_dropped():
    job = BatchExtractionJob(
        batch_id="batch_1",
        index_map={"c0q0": ("contract-a", "Parties"), "c0q1": ("contract-a", "Governing Law")},
        warmup_clauses=[],
    )
    batch_results = [
        _errored("c0q0"),
        _succeeded("c0q1", [_citation("Delaware", 0, 8)]),
    ]
    clauses, errors = wait_and_collect(job, client=_FakeClient(batch_results), poll_interval_seconds=0)

    assert len(clauses) == 1  # only the succeeded one produced a clause
    assert len(errors) == 1
    assert "contract-a/Parties" in errors[0]
    assert "c0q0" in errors[0]


def test_no_citations_produces_no_clauses_for_that_slot():
    """A NOT_FOUND response has no citations -- should contribute nothing,
    not an empty-string clause."""
    job = BatchExtractionJob(
        batch_id="batch_1",
        index_map={"c0q0": ("contract-a", "Source Code Escrow")},
        warmup_clauses=[],
    )
    batch_results = [_succeeded("c0q0", citations=None)]
    clauses, errors = wait_and_collect(job, client=_FakeClient(batch_results), poll_interval_seconds=0)

    assert clauses == []
    assert errors == []
