#!/usr/bin/env python3
"""Build the retrieval probe gold set from documents that are already in KX.

Runs on the host that holds the text. A probe is a phrase lifted verbatim from
one chunk of one document, so a probe that is not retrieved is a retrieval
failure and nothing else.

Getting that guarantee right took three attempts, and each failure is worth
naming because they all produce a plausible-looking gold set that measures the
wrong thing:

* collapsing whitespace makes the phrase stop being a substring of the document;
* slicing a character window and then taking words leaves a fragment of a word at
  the end, and a fragment stems to nothing the index holds;
* a phrase that straddles a chunk boundary is in the document but in no single
  chunk, and full-text search matches within a chunk.

So the phrase is taken from a chunk's own text, from a single line, on word
boundaries - and then verified against the database before it is written out. A
document whose chunk yields no usable phrase is reported, not skipped silently.

    python3 build_probe_gold_set.py --selection selection.json --output gold.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

MIN_WORDS = 8
MAX_WORDS = 16
MIN_CHARS = 40


def phrase_from_line(text: str) -> str | None:
    """Take a whole-word run from the longest single line of the chunk.

    No transformation of any kind: the result is a literal substring of the input,
    which is what makes the probe honest.
    """
    lines = sorted(text.split("\n"), key=len, reverse=True)
    for line in lines:
        words = line.split(" ")
        if len(words) < MIN_WORDS:
            continue
        # Start a third of the way in: past a heading, past the first clause.
        start = max(0, len(words) // 3)
        candidate = " ".join(words[start : start + MAX_WORDS]).strip()
        if len(candidate) >= MIN_CHARS and candidate in text:
            return candidate
    return None


def build(
    connection: psycopg.Connection[dict[str, Any]], document_ids: list[str]
) -> dict[str, Any]:
    questions: list[dict[str, Any]] = []
    unusable: list[dict[str, str]] = []
    with connection.cursor() as cursor:
        for document_id in document_ids:
            cursor.execute(
                """
                WITH best AS (
                    SELECT version_id
                    FROM kx.document_versions
                    WHERE document_id = %s AND is_complete
                    ORDER BY fetched_at DESC, version_id
                    LIMIT 1
                ),
                ordered AS (
                    SELECT chunks.chunk_id, chunks.text,
                           row_number() OVER (ORDER BY chunks.ordinal) AS position,
                           count(*) OVER () AS total
                    FROM kx.chunks
                    JOIN best USING (version_id)
                )
                SELECT ordered.chunk_id, ordered.text, documents.canonical_url,
                       lower(substring(documents.canonical_url FROM '^https?://([^/:?#]+)')) AS host
                FROM ordered
                CROSS JOIN kx.documents
                WHERE documents.document_id = %s
                  AND ordered.position = greatest(1, ordered.total / 2)
                """,
                (document_id, document_id),
            )
            row = cursor.fetchone()
            if row is None:
                unusable.append({"documentId": document_id, "why": "no complete chunked version"})
                continue
            phrase = phrase_from_line(str(row["text"]))
            if phrase is None:
                unusable.append(
                    {
                        "documentId": document_id,
                        "canonicalUrl": str(row["canonical_url"]),
                        "why": "no single line of the middle chunk holds a long enough phrase",
                    }
                )
                continue
            # Prove it against the database rather than trusting the string work.
            cursor.execute(
                "SELECT 1 FROM kx.chunks WHERE chunk_id = %s AND position(%s in text) > 0",
                (row["chunk_id"], phrase),
            )
            if cursor.fetchone() is None:
                unusable.append(
                    {
                        "documentId": document_id,
                        "canonicalUrl": str(row["canonical_url"]),
                        "why": "the phrase did not verify against its own chunk",
                    }
                )
                continue
            host = str(row["host"]).replace(".", "-")
            questions.append(
                {
                    "questionId": f"probe-{host}-{len(questions):02d}",
                    "kind": "probe",
                    "scope": "current",
                    "question": phrase,
                    "expectedDocuments": [document_id],
                    "note": f"verbatim from one chunk of {row['canonical_url']}",
                }
            )
    return {"questions": questions, "unusable": unusable}


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_probe_gold_set")
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", default="vertical-slice-probes")
    parser.add_argument("--dsn", default=os.environ.get("RADAR_KX_DSN", ""))
    args = parser.parse_args()

    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    document_ids = [item["documentId"] for item in selection["documents"]]
    with psycopg.connect(args.dsn, row_factory=dict_row) as connection:
        built = build(connection, document_ids)

    payload = {
        "name": args.name,
        "purpose": (
            "Retrieval probes over the vertical-slice documents. Each phrase is a "
            "verbatim run from one chunk, verified against the database, so a probe "
            "that is not retrieved is a retrieval failure. A probe measures whether "
            "the index finds text it holds; it does not measure whether the system "
            "understands a question."
        ),
        "sourceSelection": selection.get("name", str(args.selection)),
        "unusable": built["unusable"],
        "questions": built["questions"],
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "questions": len(built["questions"]),
                "unusable": len(built["unusable"]),
                "output": str(args.output),
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
