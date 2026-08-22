"""Measure retrieval against a gold set: Recall@k, MRR, p95 latency.

Plan §13.1 asks for three retrieval numbers and says the bar is set by the owner
**after** they are measured and **before** anything scales (P29). This produces
the numbers. It deliberately does not carry a pass mark: a threshold invented here
would become the bar by default, which is exactly the failure P29 names.

A gold question is a question, the documents that answer it, and why they were
chosen. Two kinds live side by side and are counted separately:

``probe``
    a distinctive phrase lifted from one document. Mechanical to author, exact by
    construction, and it measures whether the index can find text it holds. It
    does not measure whether the system understands a question.
``question``
    a real question with the documents a person judged to answer it. This is the
    number that matters, and it cannot be generated - it is authored.

Reporting them together as one score would flatter the system, so the report
breaks them out and refuses to average across kinds.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_K = 10


class Searcher(Protocol):
    def __call__(
        self, query: str, *, scope: str, limit: int
    ) -> Sequence[Any]:  # pragma: no cover - structural
        ...


@dataclass(frozen=True, slots=True)
class GoldQuestion:
    question_id: str
    kind: str
    scope: str
    question: str
    #: Documents that answer it. A hit on any of them counts as found.
    expected_documents: tuple[str, ...]
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "questionId": self.question_id,
            "kind": self.kind,
            "scope": self.scope,
            "question": self.question,
            "expectedDocuments": list(self.expected_documents),
            "note": self.note,
        }


class GoldSetError(ValueError):
    """The gold set cannot be measured against."""


def load_gold_set(path: Path) -> tuple[str, tuple[GoldQuestion, ...]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GoldSetError("gold set must be an object")
    name = str(payload.get("name") or path.stem)
    raw = payload.get("questions")
    if not isinstance(raw, list) or not raw:
        raise GoldSetError("gold set has no questions")
    questions: list[GoldQuestion] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GoldSetError(f"question {index} is not an object")
        question_id = str(item.get("questionId") or "")
        kind = str(item.get("kind") or "")
        expected = item.get("expectedDocuments")
        if not question_id or question_id in seen:
            raise GoldSetError(f"question {index} has a missing or repeated id")
        if kind not in {"probe", "question"}:
            raise GoldSetError(f"{question_id}: kind must be probe or question")
        if not isinstance(expected, list) or not expected:
            raise GoldSetError(f"{question_id}: expectedDocuments must be a non-empty array")
        seen.add(question_id)
        questions.append(
            GoldQuestion(
                question_id=question_id,
                kind=kind,
                scope=str(item.get("scope") or "current"),
                question=str(item["question"]),
                expected_documents=tuple(str(value) for value in expected),
                note=str(item.get("note") or ""),
            )
        )
    return name, tuple(questions)


@dataclass(frozen=True, slots=True)
class QuestionResult:
    question: GoldQuestion
    rank: int | None
    latency_ms: float
    returned: int

    @property
    def found(self) -> bool:
        return self.rank is not None

    def as_json(self) -> dict[str, Any]:
        return {
            "questionId": self.question.question_id,
            "kind": self.question.kind,
            "rank": self.rank,
            "found": self.found,
            "latencyMs": round(self.latency_ms, 1),
            "returned": self.returned,
        }


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile. Exact on small samples, where interpolation lies."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = min(len(ordered), max(1, math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def summarize(results: Sequence[QuestionResult], *, k: int) -> dict[str, Any]:
    def block(subset: Sequence[QuestionResult]) -> dict[str, Any]:
        if not subset:
            return {"questions": 0}
        found = [item for item in subset if item.found]
        return {
            "questions": len(subset),
            "found": len(found),
            f"recallAt{k}": round(len(found) / len(subset), 4),
            "mrr": round(sum(1 / item.rank for item in found if item.rank) / len(subset), 4),
            "latencyP50Ms": round(_percentile([item.latency_ms for item in subset], 0.50), 1),
            "latencyP95Ms": round(_percentile([item.latency_ms for item in subset], 0.95), 1),
            "notFound": [item.question.question_id for item in subset if not item.found],
        }

    return {
        "k": k,
        # Never averaged across kinds: a probe measures the index, a question
        # measures the system, and one number covering both hides which is weak.
        "byKind": {
            kind: block([item for item in results if item.question.kind == kind])
            for kind in ("probe", "question")
        },
        "thresholds": (
            "not set here. Plan P29: the bar is set by the owner from these numbers, "
            "before scaling past the vertical slice"
        ),
    }


def evaluate(
    search: Searcher, questions: Sequence[GoldQuestion], *, k: int = DEFAULT_K
) -> tuple[tuple[QuestionResult, ...], dict[str, Any]]:
    results: list[QuestionResult] = []
    for question in questions:
        started = time.perf_counter()
        hits = search(question.question, scope=question.scope, limit=k)
        latency_ms = (time.perf_counter() - started) * 1000
        expected = set(question.expected_documents)
        rank: int | None = None
        seen: list[str] = []
        for hit in hits:
            document = str(hit.document_id)
            if document in seen:
                continue
            seen.append(document)
            if document in expected:
                rank = len(seen)
                break
        results.append(
            QuestionResult(question=question, rank=rank, latency_ms=latency_ms, returned=len(hits))
        )
    return tuple(results), summarize(results, k=k)
