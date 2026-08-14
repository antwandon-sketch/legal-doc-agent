"""LLM structured extraction using the Anthropic Citations API.

Rather than prompting Claude to reproduce quotes manually, each contract is
sent as a `document` content block with `citations.enabled=true`. Claude's
response comes back as a sequence of text blocks, some of which carry a
`citations` array with `char_location` entries -- structured
{cited_text, start_char_index, end_char_index} pointing at the source
document. Those offsets are what get validated in
src/validate/citation_check.py.

Two calling modes are implemented (see PROJECT.md "LLM extraction:
per-category vs batched" for the tradeoff writeup):

- `extract_contract_per_category`: one API call per clause category (41
  calls/contract). Simplest response to parse -- unambiguous since only one
  category is in play -- and the document block is cache_control'd so only
  the first call per contract pays full input-token price for the document.
- `extract_contract_batched`: one API call per contract, asking Claude to
  work through every category and answer under a heading per category.
  Cheaper in call count, but citation-to-category attribution depends on
  parsing Claude's own generated headings out of the uncited text blocks,
  which is inherently less robust than the per-category path.

Citations cannot be combined with the API's structured-outputs feature
(output_config.format) -- see the "Citations and structured outputs are
incompatible" note in the Citations docs -- so structure here comes from
prompting Claude to answer in a fixed textual shape and parsing that, not
from a JSON schema constraint.
"""
import re

import anthropic

from src.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from src.extract.schema import ExtractedClause

MAX_TOKENS = 4096
# No temperature/top_p/top_k here, deliberately: ANTHROPIC_MODEL is a
# claude-sonnet-5-family model, and that generation rejects sampling
# parameters outright (400 "temperature is deprecated for this model") --
# confirmed against the live API, see PROJECT.md. There is no supported way
# to pin determinism via sampling params on this model generation.

# Citations API does not return a numeric confidence score -- these are a
# documented heuristic, not a model-reported probability. See PROJECT.md.
CONFIDENCE_SINGLE_CITATION = 0.85
CONFIDENCE_MULTI_CITATION = 0.6
CONFIDENCE_SHORT_SPAN_PENALTY = 0.15
SHORT_SPAN_CHARS = 10


def _client() -> anthropic.Anthropic:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set -- copy .env.example to .env and add your key."
        )
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _document_block(full_text: str, title: str, cache: bool = True, ttl: str | None = None) -> dict:
    block = {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": full_text},
        "title": title[:200],
        "citations": {"enabled": True},
    }
    if cache:
        cache_control = {"type": "ephemeral"}
        if ttl:
            cache_control["ttl"] = ttl
        block["cache_control"] = cache_control
    return block


def _per_category_content(doc_block: dict, question: str) -> list[dict]:
    """The (document, instruction) content list for a single category question
    -- shared by the synchronous per-category loop and the Batch API path so
    the two can never drift apart (see PROJECT.md bug #4: a prior duplication
    between the per-category and batched-prompt paths caused a real bug)."""
    return [
        doc_block,
        {
            "type": "text",
            "text": (
                f"{question}\n\nQuote only the exact contract text that "
                "directly and specifically answers this question. Do not "
                "quote text that is merely on the same general topic but "
                "does not itself answer the question -- for example, a "
                "clause that disclaims or negates a right is not an answer "
                "to a question asking whether that right is granted, and a "
                "clause about a related-but-different obligation is not an "
                "answer either. If the contract does not address this "
                "category at all, or only contains text that is thematically "
                "related but doesn't actually answer the question, reply "
                "with exactly: NOT_FOUND\n\n"
                "Examples of correctly applying this distinction (these are "
                "illustrative only, not from the contract below):\n\n"
                '1. Question about "Third Party Beneficiary" rights being '
                "granted to a non-party. The contract instead says: \"Except "
                "as set forth in Article 11, no Person other than the "
                "parties hereto shall have any right under this Agreement.\" "
                "That disclaims third-party rights rather than granting "
                "them, so the correct answer is: NOT_FOUND\n\n"
                '2. Question about "Post-Termination Services" the seller '
                "must continue providing. The contract instead says only: "
                '"The confidentiality obligations in Section 9 shall '
                'survive termination of this Agreement." That is a survival '
                "clause about confidentiality, not a service obligation "
                "after termination, so the correct answer is: NOT_FOUND\n\n"
                '3. Question about "Governing Law." The contract says: '
                '"This Agreement shall be governed by the laws of the '
                'State of Delaware." That directly answers the question, '
                "so here you SHOULD quote it -- don't withhold a genuine "
                "answer just because these examples show cases of "
                "abstaining."
            ),
        },
    ]


def _citation_field(cit, name: str):
    """`cit` is either a plain dict (from parse_batched_response's
    model_dump(), or from tests) or an SDK citation object straight off
    response.content[i].citations (extract_contract_per_category passes
    these through unconverted) -- accept either."""
    if isinstance(cit, dict):
        return cit.get(name)
    return getattr(cit, name, None)


def _citations_to_clauses(
    contract_id: str, clause_type: str, citations: list
) -> list[ExtractedClause]:
    clauses = []
    confidence = (
        CONFIDENCE_SINGLE_CITATION if len(citations) == 1 else CONFIDENCE_MULTI_CITATION
    )
    for cit in citations:
        if _citation_field(cit, "type") != "char_location":
            continue
        text = _citation_field(cit, "cited_text")
        conf = confidence
        if len(text.strip()) < SHORT_SPAN_CHARS:
            conf = max(0.0, conf - CONFIDENCE_SHORT_SPAN_PENALTY)
        clauses.append(
            ExtractedClause(
                contract_id=contract_id,
                clause_type=clause_type,
                extracted_text=text,
                source_char_start=_citation_field(cit, "start_char_index"),
                source_char_end=_citation_field(cit, "end_char_index"),
                confidence=conf,
                method="llm",
            )
        )
    return clauses


def extract_contract_per_category(
    contract_id: str,
    full_text: str,
    categories: dict[str, str],
    model: str = ANTHROPIC_MODEL,
    client: anthropic.Anthropic | None = None,
) -> list[ExtractedClause]:
    """One call per (contract, category). `categories` maps clause_type ->
    CUAD-style question text (see cuad_loader.get_category_questions)."""
    client = client or _client()
    results: list[ExtractedClause] = []

    for clause_type, question in categories.items():
        doc_block = _document_block(full_text, title=contract_id, cache=True)
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": _per_category_content(doc_block, question)}],
        )
        citations = _citations_from_content(response.content)
        results.extend(_citations_to_clauses(contract_id, clause_type, citations))

    return results


def _citations_from_content(content_blocks: list) -> list:
    """Pull every citation out of a response's (or a batch result message's)
    content blocks. Shared by the synchronous and Batch API paths."""
    return [
        cit
        for block in content_blocks
        if block.type == "text"
        for cit in (block.citations or [])
    ]


_HEADING_RE = re.compile(r"^\s*#{1,4}\s*(?P<category>.+?)\s*$", re.MULTILINE)


def _build_batched_prompt(categories: dict[str, str]) -> str:
    lines = [
        "Go through the following clause categories one at a time. For each, "
        "print a markdown heading with exactly the category name, then on the "
        "next line either quote the exact contract text that answers it, or "
        'write "None found." if the contract does not address it.',
        "",
    ]
    for clause_type, question in categories.items():
        lines.append(f'- "{clause_type}": {question.split("Details:", 1)[-1].strip()}')
    lines.append("")
    lines.append("Format each entry as:")
    lines.append("### <category name>")
    lines.append("<quoted text or \"None found.\">")
    return "\n".join(lines)


def parse_batched_response(content_blocks: list, categories: set[str]) -> dict[str, list[dict]]:
    """Pure parsing logic split out from extract_contract_batched so it can
    be unit-tested against synthetic content blocks without an API call.

    Walks the response's text blocks in order, tracking the most recent
    recognized category heading, and attributes any citations on later
    blocks to that heading -- until the next heading is seen.
    """
    by_category: dict[str, list[dict]] = {c: [] for c in categories}
    current: str | None = None

    for block in content_blocks:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text", "")
        text = text or ""

        heading_match = _HEADING_RE.search(text)
        if heading_match:
            candidate = heading_match.group("category").strip().strip('"')
            if candidate in categories:
                current = candidate

        citations = getattr(block, "citations", None)
        if citations is None and isinstance(block, dict):
            citations = block.get("citations")
        if citations and current:
            by_category[current].extend(
                cit if isinstance(cit, dict) else cit.model_dump() for cit in citations
            )

    return by_category


def extract_contract_batched(
    contract_id: str,
    full_text: str,
    categories: dict[str, str],
    model: str = ANTHROPIC_MODEL,
    client: anthropic.Anthropic | None = None,
) -> list[ExtractedClause]:
    """One call per contract covering every category. See module docstring
    for the tradeoff against extract_contract_per_category."""
    client = client or _client()
    doc_block = _document_block(full_text, title=contract_id, cache=False)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [doc_block, {"type": "text", "text": _build_batched_prompt(categories)}],
            }
        ],
    )
    by_category = parse_batched_response(response.content, set(categories))

    results: list[ExtractedClause] = []
    for clause_type, citations in by_category.items():
        results.extend(_citations_to_clauses(contract_id, clause_type, citations))
    return results
