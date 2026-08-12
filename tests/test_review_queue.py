import pytest

from src.extract.schema import ExtractedClause
from src.review import queue as queue_mod
from src.validate.citation_check import validate_batch

SOURCE = "This Agreement shall be governed by the laws of the State of Delaware. Acme Corp is the seller."


def _clause(clause_type, text, start, confidence, method="rule"):
    return ExtractedClause(
        contract_id="c1",
        clause_type=clause_type,
        extracted_text=text,
        source_char_start=start,
        source_char_end=start + len(text),
        confidence=confidence,
        method=method,
    )


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_mod, "QUEUE_FILE", tmp_path / "queue.json")
    yield


def test_route_splits_by_verification_and_confidence():
    high_conf = _clause("Governing Law", "Delaware", SOURCE.index("Delaware"), confidence=0.9)
    low_conf = _clause("Parties", "Acme Corp", SOURCE.index("Acme Corp"), confidence=0.4)
    hallucinated = _clause("Parties", "Wrong Name", SOURCE.index("Acme Corp"), confidence=0.9)

    validated = validate_batch([high_conf, low_conf, hallucinated], SOURCE)
    auto, queued = queue_mod.route(validated, SOURCE, threshold=0.75)

    assert len(auto) == 1 and auto[0].extracted_text == "Delaware"
    reasons = {item.clause_type: item.reason for item in queued}
    assert reasons["Parties"] in ("low_confidence", "unverified")
    assert len(queued) == 2


def test_route_requires_prior_validation():
    unvalidated = _clause("Governing Law", "Delaware", SOURCE.index("Delaware"), confidence=0.9)
    with pytest.raises(ValueError):
        queue_mod.route([unvalidated], SOURCE)


def test_unverified_item_can_never_be_approved():
    hallucinated = _clause("Parties", "Wrong Name", SOURCE.index("Acme Corp"), confidence=0.9)
    validated = validate_batch([hallucinated], SOURCE)
    _, queued = queue_mod.route(validated, SOURCE, threshold=0.75)
    queue_mod.enqueue(queued)

    item_id = queued[0].item_id
    assert queued[0].reason == "unverified"
    with pytest.raises(ValueError):
        queue_mod.approve(item_id)

    # still pending -- the failed approve attempt must not have mutated state
    assert queue_mod.load_queue()[0].status == "pending"


def test_low_confidence_verified_item_can_be_approved_and_becomes_a_finding():
    low_conf = _clause("Parties", "Acme Corp", SOURCE.index("Acme Corp"), confidence=0.4)
    validated = validate_batch([low_conf], SOURCE)
    _, queued = queue_mod.route(validated, SOURCE, threshold=0.75)
    queue_mod.enqueue(queued)

    approved = queue_mod.approve(queued[0].item_id, note="looks right")
    assert approved.status == "approved"

    findings = queue_mod.get_approved_findings()
    assert len(findings) == 1
    assert findings[0].extracted_text == "Acme Corp"


def test_enqueue_is_idempotent_across_reruns():
    low_conf = _clause("Parties", "Acme Corp", SOURCE.index("Acme Corp"), confidence=0.4)
    validated = validate_batch([low_conf], SOURCE)
    _, queued = queue_mod.route(validated, SOURCE, threshold=0.75)

    queue_mod.enqueue(queued)
    queue_mod.enqueue(queued)  # simulate re-running the pipeline on the same contract

    assert len(queue_mod.load_queue()) == 1
