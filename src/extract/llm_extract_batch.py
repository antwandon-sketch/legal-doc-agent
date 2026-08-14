"""Cost-optimized LLM extraction: one synchronous cache-warming call per
contract, then the remaining per-category calls submitted through the
Message Batches API (50% off token usage).

Why the warm-up call exists: prompt caching only helps a request that starts
*after* the request writing that cache entry has begun streaming a response
(see PROJECT.md's Batch/caching cost-tradeoff writeup). The Batches API
gives no ordering guarantee across a batch's requests, so submitting all 41
per-category calls for a contract as one batch risks every one of them
racing to write the same cache entry, and none reading it -- the batch
discount ends up costing more than caching would have saved. Paying for one
synchronous call per contract up front (with a 1-hour cache TTL, since batch
turnaround isn't as tight as a synchronous loop's) guarantees the cache is
warm before the batched calls read it.
"""
import time
from dataclasses import dataclass, field

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from src.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from src.extract.llm_extract import (
    MAX_TOKENS,
    _citations_from_content,
    _citations_to_clauses,
    _document_block,
    _per_category_content,
)
from src.extract.schema import ExtractedClause

WARMUP_CACHE_TTL = "1h"


def _client() -> anthropic.Anthropic:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set -- copy .env.example to .env and add your key."
        )
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


@dataclass
class BatchExtractionJob:
    batch_id: str
    # custom_id -> (contract_id, clause_type), so batch results (which arrive
    # in arbitrary order, keyed only by custom_id) can be attributed back.
    index_map: dict[str, tuple[str, str]]
    # Results from the synchronous warm-up calls, already resolved (not part
    # of the batch, so not covered by index_map).
    warmup_clauses: list[ExtractedClause] = field(default_factory=list)


def submit_batch_extraction(
    records: list,  # list[ContractRecord]
    categories: dict[str, str],
    model: str = ANTHROPIC_MODEL,
    client: anthropic.Anthropic | None = None,
) -> BatchExtractionJob:
    """Runs one synchronous warm-up call per contract (writes the document to
    a 1-hour-TTL cache entry), then submits every other (contract, category)
    pair as a Message Batches API job. Returns immediately after the batch
    is created -- does not wait for it to finish; use wait_and_collect for
    that."""
    client = client or _client()
    category_items = list(categories.items())
    warmup_clause_type, warmup_question = category_items[0]
    remaining = category_items[1:]

    warmup_clauses: list[ExtractedClause] = []
    batch_requests: list[Request] = []
    index_map: dict[str, tuple[str, str]] = {}

    for c_idx, record in enumerate(records):
        warmup_doc_block = _document_block(
            record.full_text, title=record.contract_id, cache=True, ttl=WARMUP_CACHE_TTL
        )
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": _per_category_content(warmup_doc_block, warmup_question),
                }
            ],
        )
        citations = _citations_from_content(response.content)
        warmup_clauses.extend(
            _citations_to_clauses(record.contract_id, warmup_clause_type, citations)
        )

        for cat_idx, (clause_type, question) in enumerate(remaining):
            custom_id = f"c{c_idx}q{cat_idx}"
            index_map[custom_id] = (record.contract_id, clause_type)
            doc_block = _document_block(
                record.full_text, title=record.contract_id, cache=True, ttl=WARMUP_CACHE_TTL
            )
            batch_requests.append(
                Request(
                    custom_id=custom_id,
                    params=MessageCreateParamsNonStreaming(
                        model=model,
                        max_tokens=MAX_TOKENS,
                        messages=[
                            {
                                "role": "user",
                                "content": _per_category_content(doc_block, question),
                            }
                        ],
                    ),
                )
            )

    batch = client.messages.batches.create(requests=batch_requests)
    return BatchExtractionJob(
        batch_id=batch.id, index_map=index_map, warmup_clauses=warmup_clauses
    )


def wait_and_collect(
    job: BatchExtractionJob,
    client: anthropic.Anthropic | None = None,
    poll_interval_seconds: float = 30.0,
) -> tuple[list[ExtractedClause], list[str]]:
    """Blocks until the batch finishes, then returns (clauses, errors).
    `errors` holds one message per custom_id whose result wasn't a success,
    so a partial batch failure doesn't silently vanish."""
    client = client or _client()

    while True:
        batch = client.messages.batches.retrieve(job.batch_id)
        if batch.processing_status == "ended":
            break
        time.sleep(poll_interval_seconds)

    clauses: list[ExtractedClause] = list(job.warmup_clauses)
    errors: list[str] = []

    for result in client.messages.batches.results(job.batch_id):
        contract_id, clause_type = job.index_map[result.custom_id]
        if result.result.type != "succeeded":
            errors.append(f"{contract_id}/{clause_type} ({result.custom_id}): {result.result.type}")
            continue
        citations = _citations_from_content(result.result.message.content)
        clauses.extend(_citations_to_clauses(contract_id, clause_type, citations))

    return clauses, errors
