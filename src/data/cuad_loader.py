"""Loader for CUAD's SQuAD-format JSON files.

CUAD ships train_separate_questions.json / test.json in SQuAD 2.0 format:
one "paragraph" per contract, whose "context" is the full contract text,
and one "qa" per clause category asking the model to highlight the relevant
span(s). answer_start offsets are character offsets directly into "context".
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from src.config import RAW_DIR

TRAIN_FILE = RAW_DIR / "train_separate_questions.json"
TEST_FILE = RAW_DIR / "test.json"


@dataclass(frozen=True)
class GroundTruthSpan:
    clause_type: str
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class ContractRecord:
    contract_id: str
    full_text: str
    ground_truth_spans: list[GroundTruthSpan]
    # Clause categories CUAD explicitly labeled as absent from this contract
    # (is_impossible=True, no qualifying span) -- needed to score recall/
    # precision correctly instead of just ignoring the negative case.
    absent_categories: list[str]


def _qa_id_to_category(qa_id: str) -> str:
    # id format is "{contract title}__{Category Name}"
    return qa_id.rsplit("__", 1)[-1]


def load_split(path: Path) -> Iterator[ContractRecord]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- run `python scripts/fetch_cuad.py` first."
        )
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    for entry in raw["data"]:
        contract_id = entry["title"]
        # CUAD's release has exactly one paragraph per contract (the full
        # document text); assert rather than silently concatenating in case
        # a future corpus version changes that.
        assert len(entry["paragraphs"]) == 1, (
            f"{contract_id} has {len(entry['paragraphs'])} paragraphs, expected 1 "
            "-- CUAD's context-per-contract assumption no longer holds"
        )
        paragraph = entry["paragraphs"][0]
        full_text = paragraph["context"]

        spans: list[GroundTruthSpan] = []
        absent: list[str] = []
        for qa in paragraph["qas"]:
            category = _qa_id_to_category(qa["id"])
            if qa.get("is_impossible") or not qa["answers"]:
                absent.append(category)
                continue
            for answer in qa["answers"]:
                start = answer["answer_start"]
                text = answer["text"]
                spans.append(
                    GroundTruthSpan(
                        clause_type=category,
                        text=text,
                        char_start=start,
                        char_end=start + len(text),
                    )
                )

        yield ContractRecord(
            contract_id=contract_id,
            full_text=full_text,
            ground_truth_spans=spans,
            absent_categories=absent,
        )


def load_train() -> Iterator[ContractRecord]:
    return load_split(TRAIN_FILE)


def load_test() -> Iterator[ContractRecord]:
    return load_split(TEST_FILE)


_category_questions_cache: dict[str, str] | None = None


def get_category_questions() -> dict[str, str]:
    """CUAD asks one QA-style question per clause category, e.g.:
    'Highlight the parts (if any) of this contract related to "Governing
    Law" that should be reviewed by a lawyer. Details: ...'

    Every contract in the corpus carries all 41 categories (present or
    is_impossible), so we only need to read one contract's worth of qas to
    recover the full category -> question mapping -- reusing CUAD's own
    annotation-aligned phrasing instead of writing our own prompts.
    """
    global _category_questions_cache
    if _category_questions_cache is not None:
        return _category_questions_cache

    with open(TEST_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    qas = raw["data"][0]["paragraphs"][0]["qas"]
    _category_questions_cache = {
        _qa_id_to_category(qa["id"]): qa["question"] for qa in qas
    }
    return _category_questions_cache
