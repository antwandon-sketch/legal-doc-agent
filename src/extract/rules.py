"""Deterministic (regex/rule-based, no LLM call) extraction.

Targets the fields that are pattern-matchable from document structure
rather than requiring language understanding: party names (preamble +
signature-block anchors), effective/execution dates, and governing law.
Every match's offsets come directly from the regex match span in
`full_text`, so by construction the extracted_text is always an exact
substring at the claimed offset -- the citation validator
(src/validate/citation_check.py) still re-checks it, since these
extractors feed into the same schema and validation path as the LLM
extractor, but a rule-based match can never fail that check.
"""
import re

from src.extract.schema import ExtractedClause

MONTHS = (
    r"January|February|March|April|May|June|July|"
    r"August|September|October|November|December"
)

# Only the date value is captured (not the surrounding anchor phrase), to
# align with how CUAD ground truth spans are annotated (date text only).
_DATE = (
    rf"(?:{MONTHS})\s+\d{{1,2}},?\s+\d{{4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+day\s+of\s+(?:{MONTHS})\s*,?\s*\d{{4}}"
    rf"|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}"
)

EFFECTIVE_DATE_RE = re.compile(
    rf"(?i:effective\s+(?:as\s+of\s+|date\s+of\s+|on\s+)|"
    rf"shall\s+be(?:come)?\s+effective\s+(?:as\s+of\s+|on\s+)?)"
    rf"(?P<date>{_DATE})"
)

AGREEMENT_DATE_RE = re.compile(
    rf"(?i:made\s+and\s+entered\s+into\s+(?:as\s+of\s+|on\s+)?|"
    rf"entered\s+into\s+(?:as\s+of\s+|on\s+)?|"
    rf"dated\s+(?:as\s+of\s+)?)"
    rf"(?P<date>{_DATE})"
)

# Contract dates almost always appear in the preamble; searching the whole
# document risks matching unrelated dates deep in boilerplate (notice
# periods, renewal terms, etc).
PREAMBLE_WINDOW = 4000

# Jurisdiction names can include an internal apostrophe ("People's Republic
# of China") and lowercase connector words ("Republic of China", "Isle of
# Man") -- an earlier version only allowed [a-zA-Z]+ tokens with no
# apostrophe or connector, which truncated "People's Republic of China" to
# just "People" (the apostrophe broke the first token, and "of"/"the" being
# lowercase broke the multi-word continuation). See PROJECT.md bug log.
GOVERNING_LAW_RE = re.compile(
    r"(?i:govern(?:ed|ing))\b[^.]{0,120}?(?i:laws\s+of)\s+"
    r"(?:(?i:the)\s+)?(?:(?i:state|commonwealth|province)\s+of\s+)?"
    r"(?P<jurisdiction>[A-Z][a-zA-Z']+(?:\s+(?:(?i:of|the)\s+)?[A-Z][a-zA-Z']+){0,3})"
)

# Each token is a capitalized word with at most one trailing period (for
# abbreviations like "Inc." / "Corp."); comma is deliberately NOT part of
# the token class. Comma/period previously being valid *inside* a greedy
# token meant a real delimiter right after "Inc." could get silently
# swallowed into the token instead of ending the match, so the reluctant
# outer repetition kept marching token-by-token past sentence boundaries
# until it found some later, unrelated "(" -- see PROJECT.md bug log.
_PARTY_TOKEN = r"[A-Z][A-Za-z0-9&'\-]*\.?"
_PARTY_NAME = rf"{_PARTY_TOKEN}(?:\s+{_PARTY_TOKEN}){{0,6}}"

PARTIES_PREAMBLE_RE = re.compile(
    rf"(?i:by\s+and\s+between|between)\s+"
    rf"(?P<party1>{_PARTY_NAME})"
    rf"\s*(?:\([^)]*\))?\s*,?\s+(?i:and)\s+"
    rf"(?P<party2>{_PARTY_NAME})"
    rf"\s*(?:\(|,|\.)"
)

# Signature-block convention: entity name on its own line immediately
# preceding a "By:" execution line.
SIGNATURE_BLOCK_RE = re.compile(
    r"^[ \t]*(?P<party>[A-Z][A-Z0-9,.&'\- ]{2,80})[ \t]*\r?\n"
    r"[ \t]*By\s*:",
    re.MULTILINE,
)


def _clause(contract_id: str, clause_type: str, match: re.Match, group: str, confidence: float) -> ExtractedClause:
    start, end = match.span(group)
    return ExtractedClause(
        contract_id=contract_id,
        clause_type=clause_type,
        extracted_text=match.group(group),
        source_char_start=start,
        source_char_end=end,
        confidence=confidence,
        method="rule",
    )


def extract_dates(contract_id: str, full_text: str) -> list[ExtractedClause]:
    window = full_text[:PREAMBLE_WINDOW]
    results = []

    m = EFFECTIVE_DATE_RE.search(window)
    if m:
        results.append(_clause(contract_id, "Effective Date", m, "date", confidence=0.85))

    m = AGREEMENT_DATE_RE.search(window)
    if m:
        results.append(_clause(contract_id, "Agreement Date", m, "date", confidence=0.75))

    return results


def extract_governing_law(contract_id: str, full_text: str) -> list[ExtractedClause]:
    m = GOVERNING_LAW_RE.search(full_text)
    if not m:
        return []
    return [_clause(contract_id, "Governing Law", m, "jurisdiction", confidence=0.8)]


def extract_parties(contract_id: str, full_text: str) -> list[ExtractedClause]:
    results = []
    seen_spans = set()

    m = PARTIES_PREAMBLE_RE.search(full_text[:PREAMBLE_WINDOW])
    if m:
        for group in ("party1", "party2"):
            clause = _clause(contract_id, "Parties", m, group, confidence=0.8)
            seen_spans.add((clause.source_char_start, clause.source_char_end))
            results.append(clause)

    # Signature block scan is a fallback/supplement: only keep matches that
    # don't overlap something the preamble pattern already found, and cap
    # at a handful to avoid pulling in unrelated all-caps section headers.
    for m in list(SIGNATURE_BLOCK_RE.finditer(full_text))[:4]:
        clause = _clause(contract_id, "Parties", m, "party", confidence=0.6)
        if any(
            clause.source_char_start < e and s < clause.source_char_end
            for s, e in seen_spans
        ):
            continue
        seen_spans.add((clause.source_char_start, clause.source_char_end))
        results.append(clause)

    return results


def extract_all(contract_id: str, full_text: str) -> list[ExtractedClause]:
    return [
        *extract_parties(contract_id, full_text),
        *extract_dates(contract_id, full_text),
        *extract_governing_law(contract_id, full_text),
    ]
