#!/usr/bin/env python3
"""Backfill Radar issues from historical DOCX/Markdown reports into SQLite."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from docx import Document

from radar_paths import (
    DB_PATH,
    NORMALIZED_ISSUES_DIR,
    PARSED_DOCX_DIR,
    RAW_DOCX_DIR,
    SOURCE_METADATA_DIR,
    WORKSPACE_CORPUS,
    ensure_dirs,
)
from agpm_radar_report import relevance_review
from agpm_radar_signal_strength import signal_strength_from_score


PERIMETER_BY_SECTION = {
    "Дальний периметр": "far",
    "Средний периметр": "mid",
    "Близкий периметр": "near",
}

MONTHS_RU = {
    "января": "January",
    "янв": "January",
    "февраля": "February",
    "фев": "February",
    "марта": "March",
    "мар": "March",
    "апреля": "April",
    "апр": "April",
    "мая": "May",
    "июня": "June",
    "июн": "June",
    "июля": "July",
    "июл": "July",
    "августа": "August",
    "авг": "August",
    "сентября": "September",
    "сен": "September",
    "октября": "October",
    "окт": "October",
    "ноября": "November",
    "ноя": "November",
    "декабря": "December",
    "дек": "December",
}

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
}

DATE_PATTERNS = [
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+20\d{2}\b",
    r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?,?\s+20\d{2}\b",
    r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b",
    r"\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+20\d{2}\b",
]

LABELED_DATE_PATTERNS = [
    r"\b(?:Released|Published|Publication date|Date)\s*:\s*(\d{1,2}/\d{1,2}/20\d{2})\b",
    r"\b(?:Released|Published|Publication date|Date)\s*:\s*([A-Z][a-z]+\.?\s+\d{1,2},\s+20\d{2})\b",
]


@dataclass
class ParsedMaterial:
    issue_date: str
    section: str
    perimeter: str
    title: str
    url: str
    summary: str
    agpm_takeaway: str
    md_source_path: str | None
    docx_source_path: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<<<EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>", " ", value)
    value = re.sub(r"<<<END_EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>", " ", value)
    value = re.sub(r"<<\s*>>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    query = [(k, v) for k, v in query if k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, netloc, path, urllib.parse.urlencode(query), ""))


def material_id(url: str, title: str) -> str:
    basis = canonicalize_url(url) or clean_text(title).lower()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def issue_date_from_path(path: Path) -> str | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    return match.group(1) if match else None


def load_materials_index(path: Path) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            url = canonicalize_url(row.get("url") or row.get("canonical_url") or "")
            if url:
                items[url] = row
    return items


def signal_for_material(parsed: ParsedMaterial, indexed: dict[str, Any], canonical: str) -> tuple[int, str]:
    raw_excerpt = clean_text(" ".join(part for part in [parsed.summary, indexed.get("raw_excerpt") or ""] if part))
    if not indexed and not raw_excerpt:
        raw_excerpt = parsed.summary
    review_item = {
        **indexed,
        "title": parsed.title,
        "url": parsed.url,
        "canonical_url": canonical,
        "summary": parsed.summary or indexed.get("summary") or "",
        "raw_excerpt": raw_excerpt,
        "perimeter": parsed.perimeter,
        "source_name": indexed.get("source_name") or source_name_from_url(parsed.url),
    }
    review = relevance_review(review_item)
    score = int(review.get("score") or 0)
    strength = signal_strength_from_score(score, review_item)
    return score, strength


def docx_to_text(path: Path) -> str:
    document = Document(path)
    chunks: list[str] = []
    for paragraph in document.paragraphs:
        text = clean_text(paragraph.text)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def parse_report_text(text: str, issue_date: str, md_path: Path | None, docx_path: Path | None) -> list[ParsedMaterial]:
    lines = text.splitlines()
    current_section = ""
    materials: list[ParsedMaterial] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("## "):
            current_section = line[3:].strip()
            i += 1
            continue
        if not line.startswith("### "):
            i += 1
            continue

        if current_section not in PERIMETER_BY_SECTION:
            i += 1
            continue

        title = clean_text(line[4:])
        block: list[str] = []
        i += 1
        while i < len(lines) and not lines[i].startswith("### ") and not lines[i].startswith("## "):
            block.append(lines[i])
            i += 1
        block_text = "\n".join(block)
        url_match = re.search(r"^\s*-?\s*Ссылка:\s*(\S+)", block_text, flags=re.MULTILINE)
        if not url_match:
            continue
        url = url_match.group(1).strip()
        summary = extract_field_block(block_text, ["Суть материала"], ["Вывод для AgPM", "Авторский вывод для AgPM"])
        takeaway = extract_field_block(block_text, ["Вывод для AgPM", "Авторский вывод для AgPM"], None)
        materials.append(
            ParsedMaterial(
                issue_date=issue_date,
                section=current_section,
                perimeter=PERIMETER_BY_SECTION[current_section],
                title=title,
                url=url,
                summary=summary,
                agpm_takeaway=takeaway,
                md_source_path=str(md_path) if md_path else None,
                docx_source_path=str(docx_path) if docx_path else None,
            )
        )
    return materials


def extract_field_block(text: str, labels: list[str], end_labels: list[str] | None) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    start = re.search(rf"^\s*-?\s*(?:{label_pattern}):\s*(.*)$", text, flags=re.MULTILINE)
    if not start:
        return ""
    collected = [start.group(1).strip()] if start.group(1).strip() else []
    rest = text[start.end() :].splitlines()
    stop_labels = [
        "Дата материала",
        "Ссылка",
        "Источники обнаружения",
        "Число источников",
        "Оценка релевантности",
        "Суть материала",
        "Вывод для AgPM",
        "Авторский вывод для AgPM",
    ]
    if end_labels:
        stop_labels.extend(end_labels)
    stop_pattern = re.compile(rf"^\s*-?\s*(?:{'|'.join(re.escape(label) for label in stop_labels)}):")
    for line in rest:
        if stop_pattern.search(line):
            break
        if line.strip():
            collected.append(line.strip())
    return clean_text("\n".join(collected))


def parse_date_value(value: str | None) -> str | None:
    if not value:
        return None
    raw = clean_text(value)
    if not raw:
        return None
    for ru, en in MONTHS_RU.items():
        raw = re.sub(ru, en, raw, flags=re.IGNORECASE)
    try:
        parsed = date_parser.parse(raw, fuzzy=True)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed.year < 2018 or parsed > datetime.now(timezone.utc):
        return None
    return parsed.date().isoformat()


def extract_published_at_from_human_text(value: str | None) -> tuple[str | None, str | None, float]:
    text = clean_text(value)
    if not text:
        return None, None, 0.0
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text[:2500], flags=re.IGNORECASE)
        if not match:
            continue
        parsed = parse_date_value(match.group(0))
        if parsed:
            return parsed, "human_description", 0.9
    return None, None, 0.0


def short_human_brief(value: str | None, limit: int = 420) -> str:
    text = clean_text(value)
    if not text:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if not sentences:
        sentences = [text]
    selected: list[str] = []
    for sentence in sentences[:3]:
        candidate = " ".join([*selected, sentence]).strip()
        if selected and len(candidate) > limit:
            break
        selected.append(sentence)
        if len(candidate) >= limit * 0.55:
            break
    brief = " ".join(selected).strip() or text
    if len(brief) <= limit:
        return brief
    cut = brief[:limit].rsplit(" ", 1)[0].strip()
    return f"{cut}…" if cut else brief[:limit].strip()


def metadata_cache_path(url: str) -> Path:
    digest = hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()[:24]
    return SOURCE_METADATA_DIR / f"{digest}.json"


def load_cached_source_metadata(url: str) -> dict[str, Any] | None:
    cache = metadata_cache_path(url)
    if not cache.exists():
        return None
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        return None


def extract_published_at_from_html(markup: str) -> tuple[str | None, str, float]:
    soup = BeautifulSoup(markup, "html.parser")
    selectors = [
        ("meta", {"property": "article:published_time"}, "content", "url_meta", 0.95),
        ("meta", {"property": "og:published_time"}, "content", "url_meta", 0.9),
        ("meta", {"name": "date"}, "content", "url_meta", 0.82),
        ("meta", {"name": "publish-date"}, "content", "url_meta", 0.82),
        ("meta", {"name": "pubdate"}, "content", "url_meta", 0.82),
        ("meta", {"itemprop": "datePublished"}, "content", "url_meta", 0.9),
        ("time", {}, "datetime", "url_meta", 0.75),
    ]
    for tag_name, attrs, attr, source, confidence in selectors:
        for tag in soup.find_all(tag_name, attrs=attrs):
            value = tag.get(attr) or tag.get_text(" ")
            parsed = parse_date_value(value)
            if parsed:
                return parsed, source, confidence

    for date_node in soup.select(".post-meta__date"):
        day = clean_text(date_node.select_one(".post-meta__day").get_text(" ") if date_node.select_one(".post-meta__day") else "")
        month = clean_text(date_node.select_one(".post-meta__month").get_text(" ") if date_node.select_one(".post-meta__month") else "")
        year = clean_text(date_node.select_one(".post-meta__year").get_text(" ") if date_node.select_one(".post-meta__year") else "")
        parsed = parse_date_value(" ".join(part for part in [day, month, year] if part))
        if parsed:
            return parsed, "article_date_block", 0.9

    visible = soup.get_text(" ")
    for pattern in LABELED_DATE_PATTERNS:
        match = re.search(pattern, visible, flags=re.IGNORECASE)
        if match:
            parsed = parse_date_value(match.group(1))
            if parsed:
                return parsed, "labeled_page_date", 0.88

    for script in soup.find_all("script", type=lambda value: value and "ld+json" in value):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        for item in stack:
            if not isinstance(item, dict):
                continue
            for key in ("datePublished", "dateCreated", "uploadDate"):
                parsed = parse_date_value(str(item.get(key) or ""))
                if parsed:
                    return parsed, "url_meta", 0.93

    h1 = soup.find("h1")
    if h1:
        header_text = clean_text(h1.find_parent().get_text(" ") if h1.find_parent() else h1.get_text(" "))
        for pattern in DATE_PATTERNS:
            match = re.search(pattern, header_text[:1200], flags=re.IGNORECASE)
            if match:
                parsed = parse_date_value(match.group(0))
                if parsed:
                    return parsed, "article_header", 0.78

    for pattern in DATE_PATTERNS:
        match = re.search(pattern, visible[:2000], flags=re.IGNORECASE)
        if match:
            parsed = parse_date_value(match.group(0))
            if parsed:
                return parsed, "article_lead_text", 0.72

    for pattern in DATE_PATTERNS:
        for match in re.finditer(pattern, visible, flags=re.IGNORECASE):
            parsed = parse_date_value(match.group(0))
            if parsed:
                return parsed, "page_text", 0.62
    return None, "unresolved", 0.0


def fetch_source_metadata(url: str, timeout: int = 15) -> dict[str, Any]:
    cache = metadata_cache_path(url)
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        snapshot_path = payload.get("snapshot_path")
        if payload.get("status") == "unresolved" and snapshot_path and Path(snapshot_path).exists():
            text = Path(snapshot_path).read_text(encoding="utf-8", errors="ignore")
            published_at, source, confidence = extract_published_at_from_html(text)
            if published_at:
                payload.update(
                    {
                        "extracted_published_at": published_at,
                        "extraction_source": source,
                        "confidence": confidence,
                        "status": "resolved" if confidence >= 0.7 else "low_confidence",
                    }
                )
                cache.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    now = utc_now()
    payload: dict[str, Any] = {
        "url": url,
        "canonical_url": canonicalize_url(url),
        "fetched_at": now,
        "status": "unresolved",
    }
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "AgPM Radar backfill/1.0 (+https://radar.aipractice.space)"},
        )
        payload["http_status"] = response.status_code
        payload["content_type"] = response.headers.get("content-type", "")
        response.raise_for_status()
        text = response.text
        snapshot = SOURCE_METADATA_DIR / f"{cache.stem}.html"
        snapshot.write_text(text[:1_500_000], encoding="utf-8", errors="ignore")
        published_at, source, confidence = extract_published_at_from_html(text)
        title = ""
        soup = BeautifulSoup(text, "html.parser")
        if soup.title and soup.title.string:
            title = clean_text(soup.title.string)
        payload.update(
            {
                "title": title,
                "extracted_published_at": published_at,
                "extraction_source": source,
                "confidence": confidence,
                "status": "resolved" if published_at and confidence >= 0.7 else ("low_confidence" if published_at else "unresolved"),
                "snapshot_path": str(snapshot),
            }
        )
    except Exception as exc:
        payload.update({"status": "unresolved", "error": str(exc)})
    cache.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def source_name_from_url(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def parse_report_stats(text: str) -> dict[str, int]:
    patterns = {
        "viewed": r"Просмотрено материалов:\s*(\d+)",
        "included": r"Включено в радар после смыслового отбора:\s*(\d+)",
        "cut": r"Отсеяно как нерелевантное агентному управлению:\s*(\d+)",
    }
    stats: dict[str, int] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            stats[key] = int(match.group(1))
    return stats


def upsert_issue(conn: sqlite3.Connection, issue_date: str, md_path: Path | None, docx_path: Path | None) -> None:
    title = f"AgPM daily radar {issue_date}"
    conn.execute(
        """
        INSERT INTO issues(issue_date, title, report_md_path, report_docx_path, status, updated_at)
        VALUES (?, ?, ?, ?, 'draft', datetime('now'))
        ON CONFLICT(issue_date) DO UPDATE SET
          title=excluded.title,
          report_md_path=excluded.report_md_path,
          report_docx_path=excluded.report_docx_path,
          updated_at=datetime('now')
        """,
        (issue_date, title, str(md_path) if md_path else None, str(docx_path) if docx_path else None),
    )


def upsert_daily_stats_from_report(
    conn: sqlite3.Connection,
    issue_date: str,
    parsed: list[ParsedMaterial],
    report_stats: dict[str, int],
) -> None:
    near = sum(1 for row in parsed if row.perimeter == "near")
    mid = sum(1 for row in parsed if row.perimeter == "mid")
    far = sum(1 for row in parsed if row.perimeter == "far")
    included = len(parsed)
    viewed = report_stats.get("viewed", included)
    cut = max(viewed - included, 0)
    conn.execute(
        """
        INSERT INTO daily_stats(stat_date, viewed, included, cut, near, mid, far, core, adjacent, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
        ON CONFLICT(stat_date) DO UPDATE SET
          viewed=excluded.viewed,
          included=excluded.included,
          cut=excluded.cut,
          near=excluded.near,
          mid=excluded.mid,
          far=excluded.far,
          core=excluded.core,
          adjacent=excluded.adjacent,
          updated_at=datetime('now')
        """,
        (issue_date, viewed, included, cut, near, mid, far, included),
    )


def upsert_source_metadata(conn: sqlite3.Connection, metadata: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO source_metadata(
          url, canonical_url, title, extracted_published_at, extraction_source, confidence,
          status, fetched_at, http_status, content_type, snapshot_path, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          canonical_url=excluded.canonical_url,
          title=excluded.title,
          extracted_published_at=excluded.extracted_published_at,
          extraction_source=excluded.extraction_source,
          confidence=excluded.confidence,
          status=excluded.status,
          fetched_at=excluded.fetched_at,
          http_status=excluded.http_status,
          content_type=excluded.content_type,
          snapshot_path=excluded.snapshot_path,
          error=excluded.error
        """,
        (
            metadata.get("url"),
            metadata.get("canonical_url"),
            metadata.get("title"),
            metadata.get("extracted_published_at"),
            metadata.get("extraction_source"),
            metadata.get("confidence"),
            metadata.get("status"),
            metadata.get("fetched_at"),
            metadata.get("http_status"),
            metadata.get("content_type"),
            metadata.get("snapshot_path"),
            metadata.get("error"),
        ),
    )


def upsert_material(
    conn: sqlite3.Connection,
    parsed: ParsedMaterial,
    materials_index: dict[str, dict[str, Any]],
    metadata: dict[str, Any] | None,
) -> None:
    canonical = canonicalize_url(parsed.url)
    indexed = materials_index.get(canonical, {})
    if metadata is None:
        metadata = load_cached_source_metadata(parsed.url)
    human_published, human_source, human_confidence = extract_published_at_from_human_text(
        " ".join(part for part in [parsed.summary, parsed.agpm_takeaway] if part)
    )
    fallback_published = parse_date_value(indexed.get("published_at"))
    meta_published = metadata.get("extracted_published_at") if metadata else None
    published_at = human_published or meta_published or fallback_published
    publication_source = (
        human_source
        or (metadata.get("extraction_source") if metadata and meta_published else None)
        or ("materials_jsonl" if fallback_published else None)
    )
    confidence = (
        human_confidence
        if human_published
        else (metadata.get("confidence") if metadata and meta_published else (0.45 if fallback_published else 0.0))
    )
    status = "resolved" if published_at and confidence and float(confidence) >= 0.7 else ("low_confidence" if published_at else "unresolved")
    brief = short_human_brief(parsed.summary or indexed.get("summary") or indexed.get("raw_excerpt") or "")
    source_material_id = indexed.get("id") or material_id(parsed.url, parsed.title)
    mid = hashlib.sha256(f"{source_material_id}|{parsed.issue_date}".encode("utf-8")).hexdigest()[:16]
    rejected = conn.execute(
        """
        SELECT 1 FROM rejected_materials_internal
        WHERE radar_issue_date = ? AND (canonical_url = ? OR url = ?)
        LIMIT 1
        """,
        (parsed.issue_date, canonical, parsed.url),
    ).fetchone()
    if rejected:
        return
    signal_score, signal_strength = signal_for_material(parsed, indexed, canonical)
    source_id = None
    source_name = source_name_from_url(parsed.url)
    for hit in indexed.get("source_hits", []):
        if hit.get("source_id"):
            source_id = hit.get("source_id")
            source_name = hit.get("source_title") or source_name
            break
    conn.execute(
        """
        INSERT INTO materials(
          id, title, url, canonical_url, source_name, source_id, published_at, first_seen_at,
          radar_issue_date, publication_date_source, publication_date_confidence,
          publication_date_status, perimeter, verdict, signal_score, signal_strength, summary, brief, agpm_takeaway,
          governance_flag, security_flag, human_in_the_loop_flag, pmo_flag, isup_flag, mcp_flag,
          key_material, docx_source_path, md_source_path, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'core', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(canonical_url, radar_issue_date) DO UPDATE SET
          title=excluded.title,
          source_name=excluded.source_name,
          source_id=excluded.source_id,
          published_at=excluded.published_at,
          first_seen_at=excluded.first_seen_at,
          publication_date_source=excluded.publication_date_source,
          publication_date_confidence=excluded.publication_date_confidence,
          publication_date_status=excluded.publication_date_status,
          perimeter=excluded.perimeter,
          signal_score=excluded.signal_score,
          signal_strength=excluded.signal_strength,
          summary=excluded.summary,
          brief=excluded.brief,
          agpm_takeaway=excluded.agpm_takeaway,
          governance_flag=excluded.governance_flag,
          security_flag=excluded.security_flag,
          human_in_the_loop_flag=excluded.human_in_the_loop_flag,
          pmo_flag=excluded.pmo_flag,
          isup_flag=excluded.isup_flag,
          mcp_flag=excluded.mcp_flag,
          docx_source_path=excluded.docx_source_path,
          md_source_path=excluded.md_source_path,
          updated_at=datetime('now')
        """,
        (
            mid,
            parsed.title,
            parsed.url,
            canonical,
            source_name,
            source_id,
            published_at,
            indexed.get("first_seen_at"),
            parsed.issue_date,
            publication_source,
            confidence,
            status,
            parsed.perimeter,
            signal_score,
            signal_strength,
            parsed.summary,
            brief,
            parsed.agpm_takeaway,
            int("governance" in (parsed.title + " " + parsed.summary + " " + parsed.agpm_takeaway).lower()),
            int(any(term in (parsed.title + " " + parsed.summary + " " + parsed.agpm_takeaway).lower() for term in ["security", "безопас", "access", "доступ"])),
            int(any(term in (parsed.title + " " + parsed.summary + " " + parsed.agpm_takeaway).lower() for term in ["human-in-the-loop", "human in the loop", "человек"])),
            int(any(term in (parsed.title + " " + parsed.summary + " " + parsed.agpm_takeaway).lower() for term in ["pmo", "project management", "portfolio", "проект", "портфел"])),
            int(any(term in (parsed.title + " " + parsed.summary + " " + parsed.agpm_takeaway).lower() for term in ["исуп", "pmf", "пм форсайт"])),
            int("mcp" in (parsed.title + " " + parsed.summary + " " + parsed.agpm_takeaway).lower()),
            int(parsed.perimeter == "near"),
            parsed.docx_source_path,
            parsed.md_source_path,
        ),
    )


def recalc_daily_stats(conn: sqlite3.Connection) -> None:
    dates = [row[0] for row in conn.execute("SELECT DISTINCT radar_issue_date FROM materials")]
    for date in dates:
        rows = conn.execute(
            """
            SELECT perimeter, verdict FROM materials
            WHERE radar_issue_date = ?
            """,
            (date,),
        ).fetchall()
        near = sum(1 for perimeter, _ in rows if perimeter == "near")
        mid = sum(1 for perimeter, _ in rows if perimeter == "mid")
        far = sum(1 for perimeter, _ in rows if perimeter == "far")
        core = sum(1 for _, verdict in rows if verdict == "core")
        adjacent = sum(1 for _, verdict in rows if verdict == "adjacent")
        included = len(rows)
        existing = conn.execute("SELECT viewed, cut FROM daily_stats WHERE stat_date = ?", (date,)).fetchone()
        viewed = int(existing[0]) if existing and int(existing[0]) >= included else included
        cut = max(viewed - included, 0)
        conn.execute(
            """
            INSERT INTO daily_stats(stat_date, viewed, included, cut, near, mid, far, core, adjacent, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(stat_date) DO UPDATE SET
              viewed=excluded.viewed,
              included=excluded.included,
              cut=excluded.cut,
              near=excluded.near,
              mid=excluded.mid,
              far=excluded.far,
              core=excluded.core,
              adjacent=excluded.adjacent,
              updated_at=datetime('now')
            """,
            (date, viewed, included, cut, near, mid, far, core, adjacent),
        )


def assign_issue_numbers(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT issue_date FROM issues ORDER BY issue_date").fetchall()
    for index, (issue_date,) in enumerate(rows, start=1):
        conn.execute("UPDATE issues SET issue_number = ?, updated_at = datetime('now') WHERE issue_date = ?", (index, issue_date))


def report_pairs() -> list[tuple[str, Path | None, Path | None]]:
    reports = WORKSPACE_CORPUS / "reports"
    by_date: dict[str, dict[str, Path]] = {}
    for path in reports.glob("AgPM_*_radar_*.md"):
        issue_date = issue_date_from_path(path)
        if issue_date:
            by_date.setdefault(issue_date, {})["md"] = path
    for path in RAW_DOCX_DIR.glob("AgPM_*_radar_*.docx"):
        issue_date = issue_date_from_path(path)
        if issue_date:
            by_date.setdefault(issue_date, {})["docx"] = path
    return [(date, row.get("md"), row.get("docx")) for date, row in sorted(by_date.items())]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--fetch-metadata", action="store_true", help="Fetch source pages and extract real publication dates.")
    parser.add_argument("--fetch-metadata-issue-date", help="Fetch source metadata only for one radar issue date, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=0, help="Limit materials for smoke tests.")
    parser.add_argument("--sleep", type=float, default=0.1, help="Delay between source fetches.")
    args = parser.parse_args()

    ensure_dirs()
    materials_index = load_materials_index(WORKSPACE_CORPUS / "data" / "materials.jsonl")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")
    total = 0
    try:
        if not args.limit:
            conn.execute("DELETE FROM material_rubrics")
            conn.execute("DELETE FROM material_date_quality")
            conn.execute("DELETE FROM llm_classifications")
            conn.execute("DELETE FROM materials_fts")
            conn.execute("DELETE FROM materials")
            conn.execute("DELETE FROM daily_stats")
        for issue_date, md_path, docx_path in report_pairs():
            upsert_issue(conn, issue_date, md_path, docx_path)
            text_path = PARSED_DOCX_DIR / f"{issue_date}.txt"
            if md_path and md_path.exists():
                text = md_path.read_text(encoding="utf-8")
                text_path.write_text(text, encoding="utf-8")
            elif docx_path and docx_path.exists():
                text = docx_to_text(docx_path)
                text_path.write_text(text, encoding="utf-8")
            else:
                continue
            parsed = parse_report_text(text, issue_date, md_path, docx_path)
            upsert_daily_stats_from_report(conn, issue_date, parsed, parse_report_stats(text))
            normalized_path = NORMALIZED_ISSUES_DIR / f"{issue_date}.json"
            normalized_path.write_text(
                json.dumps([row.__dict__ for row in parsed], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for row in parsed:
                metadata = None
                should_fetch_metadata = args.fetch_metadata and (
                    not args.fetch_metadata_issue_date or row.issue_date == args.fetch_metadata_issue_date
                )
                if should_fetch_metadata:
                    metadata = fetch_source_metadata(row.url)
                    upsert_source_metadata(conn, metadata)
                    time.sleep(args.sleep)
                else:
                    metadata = load_cached_source_metadata(row.url)
                    if metadata:
                        upsert_source_metadata(conn, metadata)
                upsert_material(conn, row, materials_index, metadata)
                total += 1
                if args.limit and total >= args.limit:
                    assign_issue_numbers(conn)
                    recalc_daily_stats(conn)
                    conn.commit()
                    print(f"Backfilled {total} materials into {args.db}")
                    return 0
        assign_issue_numbers(conn)
        recalc_daily_stats(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"Backfilled {total} materials into {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
