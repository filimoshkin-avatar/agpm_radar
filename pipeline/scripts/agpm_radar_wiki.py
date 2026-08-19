#!/usr/bin/env python3
"""Update compiled wiki pages and statistics for the AgPM radar."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import agpm_radar_report as report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WIKI = ROOT / "knowledge/agpm-radar"

PERIMETER_LABELS = {
    "far": "Дальний периметр",
    "middle": "Средний периметр",
    "near": "Близкий периметр",
    "watch": "Наблюдение",
    "exclude": "Исключено",
}

VERDICT_LABELS = {
    "core": "Ядро радара",
    "adjacent": "Смежные сигналы",
    "exclude": "Отсечено",
}


def parse_until(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.fromisoformat(value).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def period_items(materials: list[dict[str, Any]], since: datetime, until: datetime) -> list[dict[str, Any]]:
    return [deepcopy(item) for item in materials if report.in_period(item, since, until)]


def review_period(materials: list[dict[str, Any]], since: datetime, until: datetime) -> dict[str, Any]:
    items = period_items(materials, since, until)
    included, excluded = report.filter_for_report(items)
    included_perimeter = Counter(item["_radar_review"]["perimeter"] for item in included)
    included_verdict = Counter(item["_radar_review"]["verdict"] for item in included)
    raw_perimeter = Counter(item.get("perimeter") or "unknown" for item in items)
    sources = Counter()
    for item in items:
        for hit in item.get("source_hits", []):
            source_id = hit.get("source_id")
            if source_id:
                sources[source_id] += 1
    return {
        "since": since,
        "until": until,
        "items": items,
        "included": included,
        "excluded": excluded,
        "raw_perimeter": raw_perimeter,
        "included_perimeter": included_perimeter,
        "included_verdict": included_verdict,
        "sources": sources,
    }


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def fmt_date(value: datetime) -> str:
    return value.date().isoformat()


def stats_rows(stats: dict[str, Any]) -> list[list[Any]]:
    return [
        ["Просмотрено материалов", len(stats["items"])],
        ["Включено после смыслового отбора", len(stats["included"])],
        ["Отсечено", len(stats["excluded"])],
        ["Дальний периметр", stats["included_perimeter"].get("far", 0)],
        ["Средний периметр", stats["included_perimeter"].get("middle", 0)],
        ["Близкий периметр", stats["included_perimeter"].get("near", 0)],
        ["Ядро радара", stats["included_verdict"].get("core", 0)],
        ["Смежные сигналы", stats["included_verdict"].get("adjacent", 0)],
    ]


def is_aggregator_page(item: dict[str, Any]) -> bool:
    url = (item.get("url") or "").rstrip("/")
    title = report.clean(item.get("title")).lower()
    return url == "https://aiagentsdirectory.com/news" or "daily briefs and 7-day summary" in title


def render_period_block(name: str, stats: dict[str, Any]) -> list[str]:
    lines = [
        f"## {name}",
        "",
        f"Период: {fmt_date(stats['since'])} — {fmt_date(stats['until'])}",
        "",
        md_table(["Показатель", "Количество"], stats_rows(stats)),
        "",
    ]
    raw_rows = [
        [PERIMETER_LABELS.get(perimeter, perimeter), count]
        for perimeter, count in stats["raw_perimeter"].most_common()
    ]
    if raw_rows:
        lines.extend(["### Материалы до смыслового фильтра", "", md_table(["Первичный уровень", "Количество"], raw_rows), ""])
    if stats["sources"]:
        source_rows = [[source, count] for source, count in stats["sources"].most_common(10)]
        lines.extend(["### Топ источников", "", md_table(["Источник", "Попаданий"], source_rows), ""])
    return lines


def daily_series_rows(materials: list[dict[str, Any]], until: datetime, days: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start_date = until.date() - timedelta(days=days - 1)
    for index in range(days):
        day = start_date + timedelta(days=index)
        since = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        day_until = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc).replace(microsecond=0)
        stats = review_period(materials, since, day_until)
        rows.append(
            {
                "date": day.isoformat(),
                "total": len(stats["items"]),
                "included": len(stats["included"]),
                "excluded": len(stats["excluded"]),
                "far": stats["included_perimeter"].get("far", 0),
                "middle": stats["included_perimeter"].get("middle", 0),
                "near": stats["included_perimeter"].get("near", 0),
                "raw_far": stats["raw_perimeter"].get("far", 0),
                "raw_middle": stats["raw_perimeter"].get("middle", 0),
                "raw_near": stats["raw_perimeter"].get("near", 0),
                "raw_watch": stats["raw_perimeter"].get("watch", 0),
                "core": stats["included_verdict"].get("core", 0),
                "adjacent": stats["included_verdict"].get("adjacent", 0),
                "ai_agents_directory": stats["sources"].get("ai_agents_directory_daily", 0),
            }
        )
    return rows


def write_daily_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()) if rows else ["date"])
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(out.getvalue(), encoding="utf-8")


def render_daily_snapshot(stats: dict[str, Any], wiki: Path) -> str:
    stamp = fmt_date(stats["until"])
    report_md = Path("reports") / f"AgPM_daily_radar_{stamp}.md"
    report_docx = Path("reports") / f"AgPM_daily_radar_{stamp}.docx"
    run_log = Path("runs") / f"{stamp}.md"
    top = report.top_items([item for item in stats["included"] if not is_aggregator_page(item)], 12)

    lines: list[str] = [
        f"# Сводка AgPM-радара за {stamp}",
        "",
        "Эта страница — compiled-снимок ежедневного запуска. Она не заменяет отчёт, а фиксирует статистику и навигацию по накопленному корпусу.",
        "",
        "## Навигация",
        "",
        f"- Markdown-отчёт: [{report_md.as_posix()}](../../{report_md.as_posix()})",
        f"- DOCX-отчёт: [{report_docx.as_posix()}](../../{report_docx.as_posix()})",
        f"- Журнал запуска: [{run_log.as_posix()}](../../{run_log.as_posix()})",
        "",
        "## Статистика дня",
        "",
        md_table(["Показатель", "Количество"], stats_rows(stats)),
        "",
    ]

    if stats["included_perimeter"]:
        rows = [
            [PERIMETER_LABELS.get(perimeter, perimeter), count]
            for perimeter, count in stats["included_perimeter"].most_common()
        ]
        lines.extend(["## Включённые материалы по периметрам", "", md_table(["Уровень", "Количество"], rows), ""])

    if stats["raw_perimeter"]:
        rows = [
            [PERIMETER_LABELS.get(perimeter, perimeter), count]
            for perimeter, count in stats["raw_perimeter"].most_common()
        ]
        lines.extend(["## Первичная классификация до фильтра", "", md_table(["Уровень", "Количество"], rows), ""])

    if top:
        lines.extend(["## Ключевые материалы дня", ""])
        for item in top:
            review = item["_radar_review"]
            title = report.clean(item.get("title")) or "Без названия"
            lines.append(
                f"- {title} — {PERIMETER_LABELS.get(review.get('perimeter'), review.get('perimeter'))}; "
                f"{VERDICT_LABELS.get(review.get('verdict'), review.get('verdict'))}; {item.get('url', '')}"
            )
        lines.append("")

    if stats["sources"]:
        rows = [[source, count] for source, count in stats["sources"].most_common()]
        lines.extend(["## Источники дня", "", md_table(["Источник", "Попаданий"], rows), ""])

    return "\n".join(lines)


def render_stats_overview(materials: list[dict[str, Any]], until: datetime, history_days: int, wiki: Path) -> str:
    day_since = until - timedelta(days=1)
    week_since = until - timedelta(days=7)
    month_since = until - timedelta(days=30)
    mtd_since = until.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    periods = [
        ("Последние 24 часа", review_period(materials, day_since, until)),
        ("Последние 7 дней", review_period(materials, week_since, until)),
        ("Последние 30 дней", review_period(materials, month_since, until)),
        ("С начала месяца", review_period(materials, mtd_since, until)),
    ]
    rows = daily_series_rows(materials, until, history_days)

    lines: list[str] = [
        "# Статистика AgPM-радара",
        "",
        f"Обновлено: {until.isoformat()}",
        "",
        "Страница пересчитывается из инкрементальной базы `data/materials.jsonl`. Включённые материалы считаются после того же смыслового фильтра, который используется для ежедневного DOCX-отчёта.",
        "",
    ]
    for name, stats in periods:
        lines.extend(render_period_block(name, stats))

    series_rows = [
        [
            row["date"],
            row["total"],
            row["included"],
            row["far"],
            row["middle"],
            row["near"],
            row["raw_far"],
            row["raw_middle"],
            row["raw_near"],
            row["raw_watch"],
            row["core"],
            row["adjacent"],
            row["ai_agents_directory"],
        ]
        for row in rows
    ]
    lines.extend(
        [
            f"## Дневная динамика за {history_days} дней",
            "",
            md_table(
                [
                    "Дата",
                    "Всего",
                    "Включено",
                    "Дальний",
                    "Средний",
                    "Близкий",
                    "До фильтра: дальний",
                    "До фильтра: средний",
                    "До фильтра: близкий",
                    "До фильтра: наблюдение",
                    "Core",
                    "Adjacent",
                    "AI Agents Directory",
                ],
                series_rows,
            ),
            "",
            "CSV-версия той же динамики: [daily-series.csv](daily-series.csv).",
            "",
        ]
    )
    return "\n".join(lines)


def render_monthly_compiled(materials: list[dict[str, Any]], until: datetime) -> str:
    since = until.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stats = review_period(materials, since, until)
    top_near = report.top_items(
        [item for item in stats["included"] if item["_radar_review"]["perimeter"] == "near" and not is_aggregator_page(item)],
        10,
    )
    top_middle = report.top_items(
        [item for item in stats["included"] if item["_radar_review"]["perimeter"] == "middle" and not is_aggregator_page(item)],
        10,
    )
    top_far = report.top_items(
        [item for item in stats["included"] if item["_radar_review"]["perimeter"] == "far" and not is_aggregator_page(item)],
        10,
    )

    lines: list[str] = [
        f"# Накопительная сводка AgPM-радара: {until:%Y-%m}",
        "",
        "Эта страница — compiled-слой поверх ежедневных выпусков. Она предназначена для накопления устойчивых сигналов, статистики и ссылок, которые затем можно переносить в основную wiki AgPM как методические выводы.",
        "",
        "## Статистика месяца",
        "",
        md_table(["Показатель", "Количество"], stats_rows(stats)),
        "",
    ]

    for title, items in [
        ("Близкий периметр", top_near),
        ("Средний периметр", top_middle),
        ("Дальний периметр", top_far),
    ]:
        lines.extend([f"## {title}: материалы для методического чтения", ""])
        if not items:
            lines.extend(["Пока нет материалов, прошедших смысловой фильтр.", ""])
            continue
        for item in items:
            review = item["_radar_review"]
            lines.append(
                f"- {report.clean(item.get('title')) or 'Без названия'} — "
                f"{VERDICT_LABELS.get(review.get('verdict'), review.get('verdict'))}; {item.get('url', '')}"
            )
        lines.append("")

    lines.extend(
        [
            "## Методические наблюдения",
            "",
            "- Этот раздел нужно использовать как рабочую площадку для последующего отбора устойчивых выводов в основную wiki AgPM.",
            "- Один материал не является основанием для изменения канона AgPM; повторяющиеся сигналы по governance, human-in-the-loop, агентным workflow и PMO-практикам являются основанием для отдельной методической карточки.",
            "- Статистика по периметрам показывает не «важность рынка вообще», а плотность материалов, полезных для операционализации AgPM.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update AgPM radar compiled wiki pages and statistics.")
    parser.add_argument("--wiki", type=Path, default=DEFAULT_WIKI)
    parser.add_argument("--until", help="UTC date YYYY-MM-DD. Defaults to now.")
    parser.add_argument("--history-days", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    until = parse_until(args.until)
    materials = report.load_materials(args.wiki / "data" / "materials.jsonl")

    stats_dir = args.wiki / "wiki" / "stats"
    daily_dir = args.wiki / "wiki" / "daily"
    monthly_dir = args.wiki / "wiki" / "monthly"
    stats_dir.mkdir(parents=True, exist_ok=True)
    daily_dir.mkdir(parents=True, exist_ok=True)
    monthly_dir.mkdir(parents=True, exist_ok=True)

    daily_since = until - timedelta(days=1)
    daily_stats = review_period(materials, daily_since, until)
    daily_path = daily_dir / f"{until.date().isoformat()}.md"
    daily_path.write_text(render_daily_snapshot(daily_stats, args.wiki), encoding="utf-8")

    rows = daily_series_rows(materials, until, args.history_days)
    write_daily_csv(rows, stats_dir / "daily-series.csv")
    overview_path = stats_dir / "overview.md"
    overview_path.write_text(render_stats_overview(materials, until, args.history_days, args.wiki), encoding="utf-8")

    monthly_path = monthly_dir / f"{until:%Y-%m}.md"
    monthly_path.write_text(render_monthly_compiled(materials, until), encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "daily": str(daily_path),
                "stats": str(overview_path),
                "series": str(stats_dir / "daily-series.csv"),
                "monthly": str(monthly_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
