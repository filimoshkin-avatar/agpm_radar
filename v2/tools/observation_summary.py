"""Private daily-report summaries and idempotent follow-ups for background checks."""

# ruff: noqa: S607,RUF001

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from packages.contracts.event_observation import digest, validate_report
from packages.storage.event_observations import read_observation


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    pairs = report.get("pairs", [])
    suspects = [p for p in pairs if p["prediction"]["relation"] != "different_event"]
    return {
        "status": report["status"],
        "issueDate": report["issueDate"],
        "issueHash": report["issueHash"],
        "reportId": report.get("reportId"),
        "suspectedPairs": len(suspects),
        "coverage": report.get("coverage", {}),
        "pairs": [
            {
                "relation": pair["prediction"]["relation"],
                "left": " ".join(pair["left"]["title"].split())[:120],
                "right": " ".join(pair["right"]["title"].split())[:120],
                "historical": pair["left"]["issueDate"] != pair["right"]["issueDate"],
            }
            for pair in suspects
        ],
        "reviewUrl": "https://radar.agpm.space/po/?issue="
        + report["issueDate"]
        + "#event-observations",
    }


def current_summary(root: Path, issue: dict[str, Any]) -> dict[str, Any]:
    return summarize(read_observation(root, issue))


def summary_lines(summary: dict[str, Any]) -> list[str]:
    status = summary.get("status", "pending")
    count = summary.get("suspectedPairs", 0)
    states = {
        "pending": "Проверка повторов ещё выполняется; результат пока неизвестен.",
        "partial": "Проверка повторов выполнена частично.",
        "error": "Проверка повторов не завершена из-за ошибки.",
        "complete": "Проверка повторов завершена.",
    }
    lines = [states.get(status, states["error"])]
    if count:
        lines.append(f"Подозрения на повторы: {count} пар. Нужна редакторская оценка.")
    elif status == "complete":
        lines.append("Среди проверенных пар подозрений нет; полнота поиска не измерена.")
    coverage = summary.get("coverage", {})
    if coverage:
        lines.append(f"Проверено пар: {coverage['checkedPairs']} из {coverage['candidatePairs']}.")
    for pair in summary.get("pairs", [])[:5]:
        scope = "с историей" if pair["historical"] else "внутри выпуска"
        lines.append(f"• {pair['left']} / {pair['right']} ({scope}).")
    lines.append("Редакторская проверка: " + summary["reviewUrl"])
    return lines


def notification_pending(root: Path, report: dict[str, Any]) -> bool:
    channel = os.environ.get("RADAR_V2_NOTIFY_CHANNEL")
    target = os.environ.get("RADAR_V2_NOTIFY_TARGET")
    if not channel or not target or report["status"] == "pending":
        return False
    key = digest([report["reportId"], channel, target])
    return not (root / "notifications" / f"{key}.json").exists()


def notify_result(root: Path, report: dict[str, Any]) -> None:
    """Send the daily report's late result through its configured OpenClaw destination.

    The worker holds the per-date lock. Delivery receipts prevent replay spam;
    failures leave the result eligible for delivery on the next daily retry.
    """
    validate_report(report)
    if report["status"] == "pending":
        return
    channel = os.environ.get("RADAR_V2_NOTIFY_CHANNEL")
    target = os.environ.get("RADAR_V2_NOTIFY_TARGET")
    if not channel or not target:
        return
    receipt = root / "notifications" / f"{digest([report['reportId'], channel, target])}.json"
    if receipt.exists():
        return
    message = "\n".join(
        [
            f"Радар V2: проверка повторов выпуска за {report['issueDate']}.",
            "",
            *summary_lines(summarize(report)),
        ]
    )
    try:
        result = subprocess.run(  # noqa: S603 -- existing configured daily destination
            [
                "openclaw",
                "message",
                "send",
                "--channel",
                channel,
                "--target",
                target,
                "--message",
                message,
            ],
            capture_output=True,
            timeout=45,
            check=False,
        )
        if result.returncode == 0:
            receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            receipt.write_text(json.dumps({"reportId": report["reportId"]}))
            receipt.chmod(0o600)
    except (OSError, subprocess.TimeoutExpired):
        return
