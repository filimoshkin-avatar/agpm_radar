"""Content-bound owner corrections, queued for the existing V2 publisher."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LABELS = {"same_event", "different_event", "development", "insufficient_evidence"}
LOCK = threading.Lock()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def preview(
    payload: dict[str, Any], report: dict[str, Any], issue: dict[str, Any]
) -> dict[str, Any]:
    if (
        payload.get("reportId") != report.get("reportId")
        or not report.get("reportId")
        or payload.get("issueHash") != report.get("issueHash")
        or digest(issue) != report.get("issueHash")
    ):
        raise ValueError("Выпуск или отчёт изменился. Обновите страницу и повторите оценку.")
    labels = payload.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise ValueError("Сначала укажите редакторскую оценку.")
    pairs = {pair["pairId"]: pair for pair in report["pairs"]}
    if any(key not in pairs or value not in LABELS for key, value in labels.items()):
        raise ValueError("Оценки не принадлежат этому отчёту.")
    allowed = {
        card["materialId"]
        for key, label in labels.items()
        if label == "same_event"
        for card in (pairs[key]["left"], pairs[key]["right"])
        if card["issueDate"] == issue["issueDate"]
    }
    remove = payload.get("removeMaterialIds")
    if (
        not isinstance(remove, list)
        or not remove
        or any(not isinstance(x, str) for x in remove)
        or len(set(remove)) != len(remove)
        or not set(remove) <= allowed
    ):
        raise ValueError("Исключать можно только карточки текущего выпуска с оценкой «Дубль».")
    cards = issue["materials"]
    if not set(remove) <= {card["id"] for card in cards}:
        raise ValueError("Карточки больше нет в выпуске.")
    keep = [card for card in cards if card["id"] not in remove]
    if not keep:
        raise ValueError("Нельзя исключить все карточки выпуска.")
    for key, label in labels.items():
        pair = pairs[key]
        if label == "same_event" and all(
            card["issueDate"] == issue["issueDate"] and card["materialId"] in remove
            for card in (pair["left"], pair["right"])
        ):
            raise ValueError("Оставьте хотя бы одну карточку подтверждённого события.")
    return {
        "issueDate": issue["issueDate"],
        "issueHash": report["issueHash"],
        "reportId": report["reportId"],
        "labels": labels,
        "removeMaterialIds": sorted(remove),
        "removed": [{"id": c["id"], "title": c["title"]} for c in cards if c["id"] in remove],
        "retained": [{"id": c["id"], "title": c["title"]} for c in keep],
    }


class ReviewQueue:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise ValueError("invalid review id")
        return self.root / (key + ".json")

    def _write(self, key: str, value: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(key)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def submit(self, value: dict[str, Any], actor: str) -> dict[str, Any]:
        key = digest({"preview": value, "actor": actor})
        with LOCK:
            if self._path(key).exists():
                existing = self.get(key)
                if existing["status"] != "failed":
                    return existing
                for item in self.list():
                    if item["issueDate"] == value["issueDate"] and item["status"] in {
                        "queued",
                        "running",
                    }:
                        raise ValueError("Для этого выпуска уже выполняется коррекция.")
                existing.update(
                    status="queued",
                    retry=existing.get("retry", 0) + 1,
                    detail="",
                    updatedAt=datetime.now(UTC).isoformat(),
                )
                self._write(key, existing)
                return existing
            for item in self.list():
                if item["issueDate"] == value["issueDate"] and item["status"] in {
                    "queued",
                    "running",
                }:
                    raise ValueError("Для этого выпуска уже выполняется коррекция.")
            entry = {
                **value,
                "reviewId": key,
                "actor": actor,
                "status": "queued",
                "createdAt": datetime.now(UTC).isoformat(),
            }
            self._write(key, entry)
            return entry

    def get(self, key: str) -> dict[str, Any]:
        try:
            value: dict[str, Any] = json.loads(self._path(key).read_bytes())
            return value
        except FileNotFoundError:
            raise KeyError("Коррекция не найдена") from None

    def list(self) -> list[dict[str, Any]]:
        return [self.get(path.stem) for path in sorted(self.root.glob("*.json"))]

    def update(self, key: str, status: str, detail: str) -> dict[str, Any]:
        if status not in {"running", "published", "failed"} or len(detail) > 1000:
            raise ValueError("invalid review status")
        with LOCK:
            entry = self.get(key)
            if entry["status"] in {"published", "failed"}:
                return entry
            if status == "published" and entry["status"] != "running":
                raise ValueError("review has not started")
            entry.update(status=status, detail=detail, updatedAt=datetime.now(UTC).isoformat())
            self._write(key, entry)
            return entry
