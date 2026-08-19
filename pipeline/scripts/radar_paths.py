#!/usr/bin/env python3
"""Shared paths for the Radar project on /mnt/vdd."""

from __future__ import annotations

import os
from pathlib import Path


RADAR_ROOT = Path(os.environ.get("RADAR_ROOT", "/mnt/vdd/Radar"))
DATA_DIR = RADAR_ROOT / "data"
CORPUS_DIR = DATA_DIR / "corpus"
WORKSPACE_CORPUS = CORPUS_DIR / "knowledge-agpm-radar"
RAW_DOCX_DIR = CORPUS_DIR / "raw-docx"
PARSED_DOCX_DIR = CORPUS_DIR / "parsed-docx"
NORMALIZED_ISSUES_DIR = CORPUS_DIR / "normalized-issues"
SOURCE_METADATA_DIR = CORPUS_DIR / "source-metadata"
REJECTED_INTERNAL_DIR = CORPUS_DIR / "rejected-internal"
LLM_CLASSIFICATION_DIR = CORPUS_DIR / "llm-classification"
DB_PATH = Path(os.environ.get("RADAR_DB", str(DATA_DIR / "db" / "radar.sqlite")))
MIGRATIONS_DIR = DATA_DIR / "db" / "migrations"
JSON_CACHE_DIR = DATA_DIR / "exports" / "json-cache"
PIPELINE_LOG_DIR = DATA_DIR / "logs" / "pipeline"


def ensure_dirs() -> None:
    for path in [
        WORKSPACE_CORPUS,
        RAW_DOCX_DIR,
        PARSED_DOCX_DIR,
        NORMALIZED_ISSUES_DIR,
        SOURCE_METADATA_DIR,
        REJECTED_INTERNAL_DIR,
        LLM_CLASSIFICATION_DIR,
        DB_PATH.parent,
        MIGRATIONS_DIR,
        JSON_CACHE_DIR,
        PIPELINE_LOG_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)
