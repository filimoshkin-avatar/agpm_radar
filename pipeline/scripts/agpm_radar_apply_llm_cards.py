#!/usr/bin/env python3
"""Apply generated LLM card texts to the daily Markdown and DOCX report."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from agpm_radar_report import add_markdown_to_docx
from radar_paths import DB_PATH


def load_cards(conn: sqlite3.Connection, issue_date: str) -> dict[str, dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT m.title, m.brief, m.agpm_takeaway,
               s.short_text, s.agpm_angle, s.status, s.error, s.model, s.prompt_version
        FROM materials m
        LEFT JOIN material_llm_summaries s
          ON s.material_id = m.id AND s.issue_date = m.radar_issue_date
        WHERE m.radar_issue_date = ?
        ORDER BY m.title
        """,
        (issue_date,),
    ).fetchall()
    return {str(row["title"]): dict(row) for row in rows}


def is_success(card: dict[str, Any]) -> bool:
    return bool(
        card.get("status") == "success"
        and str(card.get("short_text") or "").strip()
        and str(card.get("agpm_angle") or "").strip()
    )


def replace_section(lines: list[str], label_index: int, stop_label: str, text: str) -> int:
    start = label_index + 1
    while start < len(lines) and not lines[start].strip():
        start += 1
    end = start
    while end < len(lines) and lines[end].strip() != stop_label:
        end += 1
    replacement = ["", text, ""]
    lines[start:end] = replacement
    return start + len(replacement)


def apply_cards(markdown: str, cards: dict[str, dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    lines = markdown.splitlines()
    old_control = next(
        (pos for pos, line in enumerate(lines) if line.strip() == "## Контроль генерации карточек"),
        None,
    )
    if old_control is not None:
        old_end = next(
            (pos for pos in range(old_control + 1, len(lines)) if lines[pos].startswith("## ")),
            len(lines),
        )
        del lines[old_control:old_end]
    fallback_by_title: dict[str, dict[str, str]] = {}
    successful_titles: set[str] = set()
    index = 0
    while index < len(lines):
        if not lines[index].startswith("### "):
            index += 1
            continue
        title = lines[index][4:].strip()
        card = cards.get(title)
        if not card:
            index += 1
            continue
        summary_label = next(
            (pos for pos in range(index + 1, min(index + 12, len(lines))) if lines[pos].strip() == "Суть материала:"),
            None,
        )
        if summary_label is None:
            index += 1
            continue
        takeaway_label = next(
            (pos for pos in range(summary_label + 1, min(summary_label + 12, len(lines))) if lines[pos].strip() == "Вывод для AgPM:"),
            None,
        )
        if takeaway_label is None:
            index += 1
            continue
        if is_success(card):
            next_index = replace_section(lines, summary_label, "Вывод для AgPM:", str(card["short_text"]).strip())
            takeaway_label = next(
                pos for pos in range(summary_label + 1, next_index + 2) if lines[pos].strip() == "Вывод для AgPM:"
            )
            start = takeaway_label + 1
            while start < len(lines) and not lines[start].strip():
                start += 1
            end = start
            while end < len(lines) and not lines[end].startswith(("### ", "## ", "- Дополнительные ссылки")):
                end += 1
            lines[start:end] = ["", str(card["agpm_angle"]).strip(), ""]
            successful_titles.add(title)
            index = start + 3
        else:
            fallback_by_title[title] = {
                "title": title,
                "error": str(card.get("error") or "LLM-результат отсутствует или неполон"),
            }
            index += 1

    total = len(cards)
    fallbacks = [fallback_by_title[title] for title in sorted(fallback_by_title)]
    control = [
        "## Контроль генерации карточек",
        "",
        f"LLM-описания карточек: {len(successful_titles)}/{total}. Детерминированный fallback: {len(fallbacks)}.",
    ]
    if fallbacks:
        control.extend(["", "Карточки с fallback:", ""])
        control.extend(f"- {item['title']}: {item['error']}" for item in fallbacks)
    insert_at = next((pos for pos, line in enumerate(lines) if line.strip() == "## Что важно для AgPM"), len(lines))
    lines[insert_at:insert_at] = [*control, ""]
    return "\n".join(lines).rstrip() + "\n", fallbacks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-date", required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--docx", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--sync-markdown", type=Path, action="append", default=[])
    parser.add_argument("--sync-docx", type=Path, action="append", default=[])
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        cards = load_cards(conn, args.issue_date)
    finally:
        conn.close()
    if not cards:
        raise RuntimeError(f"No cards found for issue {args.issue_date}")

    updated, fallbacks = apply_cards(args.markdown.read_text(encoding="utf-8"), cards)
    args.markdown.write_text(updated, encoding="utf-8")
    add_markdown_to_docx(updated, args.docx)
    for target in args.sync_markdown:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.markdown, target)
    for target in args.sync_docx:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.docx, target)

    success = len(cards) - len(fallbacks)
    print(json.dumps({"issue_date": args.issue_date, "total": len(cards), "success": success, "fallbacks": fallbacks}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
