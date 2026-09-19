"""Versioned, non-authoritative observations over an immutable published issue."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

FORMAT = "radar-event-observation/v1"
PROMPT_VERSION = "published-focus-pairs-v2"
RELATIONS = {
    "same_event",
    "different_event",
    "development",
    "review_overlap",
    "insufficient_evidence",
}
LABELS = {"same_event", "different_event", "development", "insufficient_evidence"}
STATUSES = {"pending", "complete", "partial", "error"}
MAX_BYTES = 2 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")


def digest(value: object) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def encode(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def issue_hash(issue: dict[str, Any]) -> str:
    """Bind to all public issue bytes, independent of JSON whitespace or key order."""
    return digest(issue)


def _text(value: Any, maximum: int = 2000) -> None:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("invalid observation text")


def validate_prediction(value: Any) -> dict[str, Any]:
    keys = {
        "relation",
        "confidence",
        "commonEvent",
        "uniqueFactsLeft",
        "uniqueFactsRight",
        "explanation",
        "evidenceLeft",
        "evidenceRight",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid prediction fields")
    if value["relation"] not in RELATIONS:
        raise ValueError("invalid relation")
    score = value["confidence"]
    if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("invalid confidence")
    for key in ("commonEvent", "explanation"):
        _text(value[key])
    for key in ("evidenceLeft", "evidenceRight"):
        _text(value[key], 250)
    for key in ("uniqueFactsLeft", "uniqueFactsRight"):
        if not isinstance(value[key], list) or len(value[key]) > 4:
            raise ValueError("invalid unique facts")
        for fact in value[key]:
            _text(fact, 500)
    return value


def validate_report(value: Any) -> dict[str, Any]:
    keys = {
        "format",
        "issueDate",
        "issueHash",
        "reportId",
        "generatedAt",
        "status",
        "model",
        "promptVersion",
        "historyDays",
        "coverage",
        "pairs",
        "errors",
    }
    if not isinstance(value, dict) or set(value) != keys or value["format"] != FORMAT:
        raise ValueError("invalid observation report")
    date.fromisoformat(value["issueDate"])
    for key in ("issueHash", "reportId"):
        if not isinstance(value[key], str) or _HASH.fullmatch(value[key]) is None:
            raise ValueError("invalid observation hash")
    if digest({k: v for k, v in value.items() if k != "reportId"}) != value["reportId"]:
        raise ValueError("observation report hash mismatch")
    if value["status"] not in STATUSES or value["historyDays"] != 45:
        raise ValueError("invalid observation status/window")
    for key in ("generatedAt", "model", "promptVersion"):
        _text(value[key], 100)
    coverage = value["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {
        "currentCards",
        "historicalCards",
        "candidatePairs",
        "checkedPairs",
    }:
        raise ValueError("invalid coverage")
    if any(type(n) is not int or not 0 <= n <= 100000 for n in coverage.values()):
        raise ValueError("invalid coverage counts")
    if not isinstance(value["errors"], list) or len(value["errors"]) > 100:
        raise ValueError("invalid errors")
    for error in value["errors"]:
        _text(error, 500)
    if not isinstance(value["pairs"], list) or len(value["pairs"]) > 100:
        raise ValueError("invalid pairs")
    seen = set()
    for pair in value["pairs"]:
        if not isinstance(pair, dict) or set(pair) != {
            "pairId",
            "left",
            "right",
            "retrievalScore",
            "prediction",
        }:
            raise ValueError("invalid pair fields")
        if (
            not isinstance(pair["pairId"], str)
            or _HASH.fullmatch(pair["pairId"]) is None
            or pair["pairId"] in seen
        ):
            raise ValueError("invalid or duplicate pair id")
        seen.add(pair["pairId"])
        if type(pair["retrievalScore"]) not in {int, float} or not 0 <= pair["retrievalScore"] <= 1:
            raise ValueError("invalid retrieval score")
        for side in ("left", "right"):
            card = pair[side]
            if not isinstance(card, dict) or set(card) != {
                "issueDate",
                "materialId",
                "title",
                "url",
                "focus",
                "contentHash",
            }:
                raise ValueError("invalid observation card")
            date.fromisoformat(card["issueDate"])
            for key in ("title", "materialId", "url", "contentHash"):
                _text(card[key])
            _text(card["focus"], 12000)
            if urlsplit(card["url"]).scheme not in {"http", "https"}:
                raise ValueError("invalid source URL")
            if digest({k: v for k, v in card.items() if k != "contentHash"}) != card["contentHash"]:
                raise ValueError("card hash mismatch")
        if pair["left"]["issueDate"] != value["issueDate"]:
            raise ValueError("pair does not belong to issue")
        if digest([pair["left"]["contentHash"], pair["right"]["contentHash"]]) != pair["pairId"]:
            raise ValueError("pair hash mismatch")
        prediction = validate_prediction(pair["prediction"])
        for side in ("Left", "Right"):
            quote = prediction["evidence" + side]
            if quote and quote not in pair[side.lower()]["focus"]:
                raise ValueError("evidence is not a published card fragment")
    if coverage["checkedPairs"] != len(value["pairs"]) or coverage["candidatePairs"] < len(
        value["pairs"]
    ):
        raise ValueError("coverage differs from predictions")
    if value["status"] == "complete" and (
        value["errors"] or coverage["checkedPairs"] != coverage["candidatePairs"]
    ):
        raise ValueError("incomplete observation claimed complete")
    if len(encode(value)) > MAX_BYTES:
        raise ValueError("observation too large")
    return value
