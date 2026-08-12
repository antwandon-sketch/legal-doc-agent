"""Unit tests for the batched-response parser in src/extract/llm_extract.py.

Uses synthetic content blocks shaped like the Anthropic SDK's response
objects (plain dicts work too, since the parser accepts both) so this runs
without an API key or network access.
"""
from src.extract.llm_extract import parse_batched_response


def _text_block(text, citations=None):
    return {"type": "text", "text": text, "citations": citations}


def test_attributes_citation_to_preceding_heading():
    categories = {"Governing Law", "Parties"}
    blocks = [
        _text_block("### Governing Law\n"),
        _text_block(
            "This Agreement is governed by the laws of Delaware.",
            citations=[
                {
                    "type": "char_location",
                    "cited_text": "governed by the laws of Delaware",
                    "start_char_index": 100,
                    "end_char_index": 133,
                }
            ],
        ),
        _text_block("### Parties\n"),
        _text_block("None found."),
    ]

    result = parse_batched_response(blocks, categories)

    assert len(result["Governing Law"]) == 1
    assert result["Governing Law"][0]["cited_text"] == "governed by the laws of Delaware"
    assert result["Parties"] == []


def test_ignores_citations_before_any_recognized_heading():
    categories = {"Governing Law"}
    blocks = [
        _text_block(
            "stray preamble text",
            citations=[
                {
                    "type": "char_location",
                    "cited_text": "should not be attributed",
                    "start_char_index": 0,
                    "end_char_index": 10,
                }
            ],
        ),
        _text_block("### Governing Law\n"),
    ]

    result = parse_batched_response(blocks, categories)

    assert result["Governing Law"] == []


def test_multiple_citations_under_one_heading_all_collected():
    categories = {"Parties"}
    blocks = [
        _text_block("### Parties\n"),
        _text_block(
            "Acme Corp",
            citations=[
                {"type": "char_location", "cited_text": "Acme Corp", "start_char_index": 5, "end_char_index": 14}
            ],
        ),
        _text_block(
            "Beta LLC",
            citations=[
                {"type": "char_location", "cited_text": "Beta LLC", "start_char_index": 30, "end_char_index": 38}
            ],
        ),
    ]

    result = parse_batched_response(blocks, categories)

    assert len(result["Parties"]) == 2
    assert {c["cited_text"] for c in result["Parties"]} == {"Acme Corp", "Beta LLC"}
