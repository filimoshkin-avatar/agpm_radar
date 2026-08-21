from __future__ import annotations

import gzip
import json
import shutil
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from radar_kx.config import Settings
from radar_kx.fetcher import DocumentTask, FetchResult
from radar_kx.identifiers import (
    PARSER_CONFIG_HASH,
    chunk_text,
    document_id,
    sha256_bytes,
    stable_json_bytes,
    version_id,
)
from radar_kx.manifest import Manifest
from radar_kx.parser import ParsedContent
from radar_kx.url_policy import normalize_url

SCHEMA_VERSION = 1


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def connect(self) -> Connection[dict[str, Any]]:
        return psycopg.connect(self.settings.dsn, row_factory=dict_row)

    def require_schema(self, connection: Connection[dict[str, Any]]) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT value FROM kx.metadata WHERE key = 'schema_version'")
            row = cursor.fetchone()
        if row is None or row["value"] != SCHEMA_VERSION:
            raise RuntimeError(f"radar-kx schema version must be {SCHEMA_VERSION}")

    def import_manifest(self, manifest: Manifest, *, source_name: str) -> dict[str, int | str]:
        document_ids = {record.document_id for record in manifest.records}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.corpus_imports (
                        corpus_sha256, source_name, row_count, document_count
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (corpus_sha256) DO NOTHING
                    """,
                    (
                        manifest.source_sha256,
                        source_name,
                        len(manifest.records),
                        len(document_ids),
                    ),
                )
                for record in manifest.records:
                    cursor.execute(
                        """
                        INSERT INTO kx.documents (
                            document_id, canonical_url, first_seen_at, last_seen_at
                        ) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (document_id) DO UPDATE SET
                            first_seen_at = CASE
                                WHEN kx.documents.first_seen_at IS NULL THEN EXCLUDED.first_seen_at
                                WHEN EXCLUDED.first_seen_at IS NULL THEN kx.documents.first_seen_at
                                ELSE LEAST(kx.documents.first_seen_at, EXCLUDED.first_seen_at)
                            END,
                            last_seen_at = CASE
                                WHEN kx.documents.last_seen_at IS NULL THEN EXCLUDED.last_seen_at
                                WHEN EXCLUDED.last_seen_at IS NULL THEN kx.documents.last_seen_at
                                ELSE GREATEST(kx.documents.last_seen_at, EXCLUDED.last_seen_at)
                            END,
                            updated_at = clock_timestamp()
                        """,
                        (
                            record.document_id,
                            record.canonical_url,
                            record.first_seen_at,
                            record.last_seen_at,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO kx.source_materials (
                            material_id, source_url, canonical_url, title, summary,
                            raw_excerpt, perimeter, published_raw, first_seen_at,
                            last_seen_at, payload, payload_sha256, corpus_sha256
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (material_id) DO UPDATE SET
                            source_url = EXCLUDED.source_url,
                            canonical_url = EXCLUDED.canonical_url,
                            title = EXCLUDED.title,
                            summary = EXCLUDED.summary,
                            raw_excerpt = EXCLUDED.raw_excerpt,
                            perimeter = EXCLUDED.perimeter,
                            published_raw = EXCLUDED.published_raw,
                            first_seen_at = EXCLUDED.first_seen_at,
                            last_seen_at = EXCLUDED.last_seen_at,
                            payload = EXCLUDED.payload,
                            payload_sha256 = EXCLUDED.payload_sha256,
                            corpus_sha256 = EXCLUDED.corpus_sha256,
                            updated_at = clock_timestamp()
                        """,
                        (
                            record.material_id,
                            record.source_url,
                            record.canonical_url,
                            record.title,
                            record.summary,
                            record.raw_excerpt,
                            record.perimeter,
                            record.published_raw,
                            record.first_seen_at,
                            record.last_seen_at,
                            Jsonb(record.payload),
                            record.payload_sha256,
                            manifest.source_sha256,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO kx.source_material_revisions (
                            material_id, corpus_sha256, document_id, source_url,
                            canonical_url, payload, payload_sha256
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (material_id, corpus_sha256) DO NOTHING
                        """,
                        (
                            record.material_id,
                            manifest.source_sha256,
                            record.document_id,
                            record.source_url,
                            record.canonical_url,
                            Jsonb(record.payload),
                            record.payload_sha256,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO kx.material_documents (material_id, document_id)
                        VALUES (%s, %s)
                        ON CONFLICT (material_id) DO UPDATE SET
                            document_id = EXCLUDED.document_id
                        """,
                        (record.material_id, record.document_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO kx.fetch_queue (document_id, status)
                        VALUES (%s, 'pending')
                        ON CONFLICT (document_id) DO NOTHING
                        """,
                        (record.document_id,),
                    )
            connection.commit()
        return {
            "corpusSha256": manifest.source_sha256,
            "materials": len(manifest.records),
            "documents": len(document_ids),
        }

    def claim_tasks(self, *, limit: int, per_host_limit: int) -> list[DocumentTask]:
        lease_token = uuid.uuid4()
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE kx.fetch_queue
                    SET status = 'retry', lease_token = NULL, lease_until = NULL,
                        attempt_count = greatest(attempt_count - 1, 0),
                        next_attempt_at = clock_timestamp(),
                        last_error_code = 'expired_lease',
                        updated_at = clock_timestamp()
                    WHERE status = 'running' AND lease_until < clock_timestamp()
                    """
                )
                cursor.execute(
                    """
                    WITH host_load AS (
                        SELECT lower(substring(documents.canonical_url
                                               FROM '^https?://([^/:?#]+)')) AS host_key,
                               count(*) AS running_count
                        FROM kx.fetch_queue AS queue
                        JOIN kx.documents AS documents USING (document_id)
                        WHERE queue.status = 'running'
                        GROUP BY host_key
                    ), ranked AS (
                        SELECT queue.document_id, queue.priority,
                               lower(substring(documents.canonical_url
                                               FROM '^https?://([^/:?#]+)')) AS host_key,
                               row_number() OVER (
                                   PARTITION BY lower(substring(
                                       documents.canonical_url
                                       FROM '^https?://([^/:?#]+)'))
                                   ORDER BY queue.priority DESC, queue.document_id
                               ) AS host_rank
                        FROM kx.fetch_queue AS queue
                        JOIN kx.documents AS documents USING (document_id)
                        WHERE queue.status IN ('pending', 'retry')
                          AND queue.next_attempt_at <= clock_timestamp()
                          AND queue.attempt_count < %s
                    ), candidates AS (
                        SELECT ranked.document_id, ranked.priority
                        FROM ranked
                        LEFT JOIN host_load USING (host_key)
                        WHERE ranked.host_rank <= GREATEST(
                            0, %s - coalesce(host_load.running_count, 0)
                        )
                        ORDER BY ranked.priority DESC, ranked.document_id
                        LIMIT %s
                    ), picked AS (
                        SELECT queue.document_id
                        FROM kx.fetch_queue AS queue
                        JOIN candidates USING (document_id)
                        ORDER BY queue.priority DESC, queue.document_id
                        FOR UPDATE OF queue SKIP LOCKED
                    )
                    UPDATE kx.fetch_queue AS queue
                    SET status = 'running',
                        attempt_count = queue.attempt_count + 1,
                        lease_token = %s,
                        lease_until = clock_timestamp() + make_interval(secs => %s),
                        updated_at = clock_timestamp()
                    FROM picked
                    WHERE queue.document_id = picked.document_id
                    """,
                    (
                        self.settings.max_attempts,
                        per_host_limit,
                        limit,
                        lease_token,
                        self.settings.lease_seconds,
                    ),
                )
                cursor.execute(
                    """
                    SELECT queue.document_id, documents.canonical_url, queue.attempt_count,
                           validators.etag, validators.last_modified
                    FROM kx.fetch_queue AS queue
                    JOIN kx.documents AS documents USING (document_id)
                    LEFT JOIN LATERAL (
                        SELECT response_headers ->> 'etag' AS etag,
                               response_headers ->> 'last-modified' AS last_modified
                        FROM kx.fetch_attempts
                        WHERE fetch_attempts.document_id = queue.document_id
                          AND source_kind = 'network'
                          AND outcome IN ('succeeded', 'not_modified')
                        ORDER BY finished_at DESC, attempt_id DESC
                        LIMIT 1
                    ) AS validators ON true
                    WHERE queue.lease_token = %s
                    ORDER BY queue.priority DESC, queue.document_id
                    """,
                    (lease_token,),
                )
                rows = cursor.fetchall()
            connection.commit()
        return [
            DocumentTask(
                document_id=str(row["document_id"]),
                canonical_url=str(row["canonical_url"]),
                attempt_count=int(row["attempt_count"]),
                etag=str(row["etag"]) if row["etag"] is not None else None,
                last_modified=(
                    str(row["last_modified"]) if row["last_modified"] is not None else None
                ),
            )
            for row in rows
        ]

    def _assert_capacity(self, stored_bytes: int) -> None:
        free = shutil.disk_usage(self.settings.capacity_path).free
        required = self.settings.min_free_bytes + stored_bytes * 3
        if free < required:
            raise RuntimeError(f"disk reserve would be violated: free={free} required={required}")

    @staticmethod
    def _insert_raw_blob(
        cursor: psycopg.Cursor[dict[str, Any]],
        *,
        body: bytes,
    ) -> tuple[str, bytes]:
        raw_sha256 = sha256_bytes(body)
        compressed = gzip.compress(body, compresslevel=6, mtime=0)
        cursor.execute(
            """
            INSERT INTO kx.raw_blobs (
                raw_sha256, compression, raw_bytes, stored_bytes, content
            ) VALUES (%s, 'gzip', %s, %s, %s)
            ON CONFLICT (raw_sha256) DO NOTHING
            """,
            (raw_sha256, len(body), len(compressed), compressed),
        )
        return raw_sha256, compressed

    @staticmethod
    def _insert_version(
        cursor: psycopg.Cursor[dict[str, Any]],
        *,
        document: str,
        raw_sha256: str,
        parsed: ParsedContent,
        source_kind: str,
        fetched_at: datetime,
    ) -> str | None:
        if not parsed.text:
            return None
        text_sha256 = sha256_bytes(parsed.text.encode("utf-8"))
        identifier = version_id(
            document=document,
            raw_sha256=raw_sha256,
            text_sha256=text_sha256,
        )
        cursor.execute(
            """
            INSERT INTO kx.document_versions (
                version_id, document_id, raw_sha256, source_kind,
                canonical_text, canonical_text_sha256, title, language,
                parser_name, parser_version, parser_config_sha256,
                quality, is_complete, fetched_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (version_id) DO NOTHING
            """,
            (
                identifier,
                document,
                raw_sha256,
                source_kind,
                parsed.text,
                text_sha256,
                parsed.title,
                parsed.language,
                parsed.parser_name,
                parsed.parser_version,
                PARSER_CONFIG_HASH,
                parsed.quality,
                parsed.is_complete,
                fetched_at,
            ),
        )
        for chunk in chunk_text(identifier, parsed.text):
            cursor.execute(
                """
                INSERT INTO kx.chunks (
                    chunk_id, version_id, ordinal, char_start, char_end,
                    text, text_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id) DO NOTHING
                """,
                (
                    chunk.chunk_id,
                    identifier,
                    chunk.ordinal,
                    chunk.char_start,
                    chunk.char_end,
                    chunk.text,
                    chunk.text_sha256,
                ),
            )
        if parsed.is_complete:
            cursor.execute(
                """
                UPDATE kx.documents
                SET best_version_id = %s, updated_at = clock_timestamp()
                WHERE document_id = %s
                  AND (
                    best_version_id IS NULL OR
                    (SELECT fetched_at FROM kx.document_versions
                     WHERE version_id = best_version_id) <= %s
                  )
                """,
                (identifier, document, fetched_at),
            )
        return identifier

    def record_fetch_result(self, result: FetchResult) -> dict[str, Any]:
        now = datetime.now(UTC)
        response = result.response
        body = response.body if response is not None else None
        if body is not None:
            compressed_size = len(gzip.compress(body, compresslevel=6, mtime=0))
            self._assert_capacity(compressed_size)
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                raw_sha256: str | None = None
                version: str | None = None
                if body is not None and response is not None:
                    raw_sha256, _ = self._insert_raw_blob(cursor, body=body)
                    if result.parsed is not None:
                        version = self._insert_version(
                            cursor,
                            document=result.task.document_id,
                            raw_sha256=raw_sha256,
                            parsed=result.parsed,
                            source_kind="network",
                            fetched_at=response.fetched_at,
                        )
                if result.not_modified:
                    outcome = "not_modified"
                elif result.parsed is not None and result.parsed.is_complete:
                    outcome = "succeeded"
                else:
                    outcome = result.error_code or "failed"
                cursor.execute(
                    """
                    INSERT INTO kx.fetch_attempts (
                        document_id, source_kind, requested_url, final_url,
                        started_at, finished_at, http_status, content_type,
                        response_headers, raw_sha256, outcome, error_detail,
                        worker_release
                    ) VALUES (
                        %s, 'network', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        result.task.document_id,
                        result.task.canonical_url,
                        response.final_url if response is not None else None,
                        response.started_at if response is not None else now,
                        now,
                        response.http_status if response is not None else None,
                        response.content_type if response is not None else None,
                        Jsonb(response.headers if response is not None else {}),
                        raw_sha256,
                        outcome,
                        result.error_detail,
                        self.settings.release_id,
                    ),
                )
                succeeded = result.not_modified or (
                    result.parsed is not None and result.parsed.is_complete
                )
                if succeeded:
                    queue_status = "succeeded"
                    next_attempt_at = now
                elif result.retryable and result.task.attempt_count < self.settings.max_attempts:
                    queue_status = "retry"
                    delay_minutes = min(24 * 60, 5 * (2 ** (result.task.attempt_count - 1)))
                    next_attempt_at = now + timedelta(minutes=delay_minutes)
                else:
                    queue_status = "failed"
                    next_attempt_at = now
                cursor.execute(
                    """
                    UPDATE kx.fetch_queue
                    SET status = %s, next_attempt_at = %s,
                        lease_token = NULL, lease_until = NULL,
                        last_http_status = %s,
                        last_error_code = %s,
                        last_error_detail = %s,
                        updated_at = clock_timestamp()
                    WHERE document_id = %s
                    """,
                    (
                        queue_status,
                        next_attempt_at,
                        response.http_status if response is not None else None,
                        result.error_code,
                        result.error_detail,
                        result.task.document_id,
                    ),
                )
            connection.commit()
        return {
            "documentId": result.task.document_id,
            "status": queue_status,
            "outcome": outcome,
            "versionId": version,
        }

    def store_cached_version(
        self,
        *,
        source_url: str,
        body: bytes,
        parsed: ParsedContent,
        source_kind: str,
        fetched_at: datetime,
        content_type: str,
        error_detail: str | None = None,
    ) -> str | None:
        if source_kind not in {"legacy_snapshot", "legacy_truncated"}:
            raise ValueError("invalid cache source kind")
        canonical_url = normalize_url(source_url)
        identifier = document_id(canonical_url)
        compressed_size = len(gzip.compress(body, compresslevel=6, mtime=0))
        self._assert_capacity(compressed_size)
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.documents (document_id, canonical_url)
                    VALUES (%s, %s)
                    ON CONFLICT (document_id) DO NOTHING
                    """,
                    (identifier, canonical_url),
                )
                raw_sha256, _ = self._insert_raw_blob(cursor, body=body)
                version = self._insert_version(
                    cursor,
                    document=identifier,
                    raw_sha256=raw_sha256,
                    parsed=parsed,
                    source_kind=source_kind,
                    fetched_at=fetched_at,
                )
                cursor.execute(
                    """
                    INSERT INTO kx.fetch_attempts (
                        document_id, source_kind, requested_url, final_url,
                        started_at, finished_at, http_status, content_type,
                        response_headers, raw_sha256, outcome, error_detail,
                        worker_release
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, 200, %s,
                        '{}'::jsonb, %s, %s, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        identifier,
                        source_kind,
                        canonical_url,
                        canonical_url,
                        fetched_at,
                        fetched_at,
                        content_type,
                        raw_sha256,
                        "succeeded" if parsed.is_complete else "partial",
                        error_detail,
                        self.settings.release_id,
                    ),
                )
            connection.commit()
        return version

    def requeue_failed(self, *, error_code: str | None = None) -> int:
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                if error_code is None:
                    cursor.execute(
                        """
                        UPDATE kx.fetch_queue
                        SET status = 'retry', attempt_count = 0,
                            next_attempt_at = clock_timestamp(),
                            lease_token = NULL, lease_until = NULL,
                            updated_at = clock_timestamp()
                        WHERE status = 'failed'
                        """
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE kx.fetch_queue
                        SET status = 'retry', attempt_count = 0,
                            next_attempt_at = clock_timestamp(),
                            lease_token = NULL, lease_until = NULL,
                            updated_at = clock_timestamp()
                        WHERE status = 'failed' AND last_error_code = %s
                        """,
                        (error_code,),
                    )
                changed = cursor.rowcount
            connection.commit()
        return changed

    def status(self) -> dict[str, Any]:
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, count(*) AS count
                    FROM kx.fetch_queue GROUP BY status ORDER BY status
                    """
                )
                queue = {str(row["status"]): int(row["count"]) for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM kx.source_materials) AS materials,
                        (SELECT count(*) FROM kx.source_material_revisions) AS material_revisions,
                        (SELECT count(*) FROM kx.documents) AS documents,
                        (SELECT count(*) FROM kx.raw_blobs) AS raw_blobs,
                        (SELECT coalesce(sum(raw_bytes), 0) FROM kx.raw_blobs) AS raw_bytes,
                        (SELECT coalesce(sum(stored_bytes), 0) FROM kx.raw_blobs) AS stored_bytes,
                        (SELECT count(*) FROM kx.document_versions) AS versions,
                        (SELECT count(*) FROM kx.document_versions
                         WHERE is_complete) AS complete_versions,
                        (SELECT count(*) FROM kx.documents
                         WHERE best_version_id IS NOT NULL) AS covered_documents,
                        (SELECT pg_database_size(current_database())) AS database_bytes
                    """
                )
                totals = cursor.fetchone()
        if totals is None:
            raise RuntimeError("status query returned no row")
        return {"queue": queue, **{key: int(value) for key, value in totals.items()}}

    def iter_failures(self, *, limit: int = 100) -> Iterator[dict[str, Any]]:
        with self.connect() as connection, connection.cursor() as cursor:
            self.require_schema(connection)
            cursor.execute(
                """
                SELECT queue.document_id, documents.canonical_url,
                       queue.attempt_count, queue.last_http_status,
                       queue.last_error_code, queue.last_error_detail
                FROM kx.fetch_queue AS queue
                JOIN kx.documents AS documents USING (document_id)
                WHERE queue.status = 'failed'
                ORDER BY queue.last_error_code, documents.canonical_url
                LIMIT %s
                """,
                (limit,),
            )
            yield from cursor.fetchall()

    def verify(self, *, full: bool) -> dict[str, Any]:
        errors: list[str] = []
        status = self.status()
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*) AS count
                    FROM kx.documents AS documents
                    JOIN kx.document_versions AS versions
                      ON versions.version_id = documents.best_version_id
                    WHERE NOT versions.is_complete
                       OR versions.document_id <> documents.document_id
                    """
                )
                row = cursor.fetchone()
                if row is not None and int(row["count"]) != 0:
                    errors.append("best_version_id violates document/complete-version contract")
            if full:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT count(*) AS count
                        FROM (
                            SELECT imports.corpus_sha256
                            FROM kx.corpus_imports AS imports
                            LEFT JOIN kx.source_material_revisions AS revisions
                              USING (corpus_sha256)
                            GROUP BY imports.corpus_sha256, imports.row_count,
                                     imports.document_count
                            HAVING count(revisions.material_id) <> imports.row_count
                                OR count(DISTINCT revisions.document_id)
                                   <> imports.document_count
                        ) AS mismatches
                        """
                    )
                    row = cursor.fetchone()
                    if row is not None and int(row["count"]) != 0:
                        errors.append("corpus import counts do not match immutable revisions")

                    cursor.execute(
                        """
                        WITH ordered AS (
                            SELECT chunks.*,
                                   versions.canonical_text,
                                   row_number() OVER (
                                       PARTITION BY chunks.version_id
                                       ORDER BY chunks.ordinal
                                   ) - 1 AS expected_ordinal,
                                   lag(chunks.char_end, 1, 0) OVER (
                                       PARTITION BY chunks.version_id
                                       ORDER BY chunks.ordinal
                                   ) AS expected_start
                            FROM kx.chunks AS chunks
                            JOIN kx.document_versions AS versions USING (version_id)
                        )
                        SELECT count(*) AS count
                        FROM ordered
                        WHERE ordinal <> expected_ordinal
                           OR char_start <> expected_start
                           OR char_end - char_start <> char_length(text)
                           OR substr(canonical_text, char_start + 1, char_end - char_start)
                              <> text
                           OR encode(digest(convert_to(text, 'UTF8'), 'sha256'), 'hex')
                              <> text_sha256
                        """
                    )
                    row = cursor.fetchone()
                    if row is not None and int(row["count"]) != 0:
                        errors.append("one or more chunks violate offset/text/hash continuity")

                    cursor.execute(
                        """
                        SELECT count(*) AS count
                        FROM kx.document_versions AS versions
                        LEFT JOIN (
                            SELECT version_id, count(*) AS chunk_count,
                                   min(char_start) AS min_start,
                                   max(char_end) AS max_end
                            FROM kx.chunks GROUP BY version_id
                        ) AS coverage USING (version_id)
                        WHERE coalesce(coverage.chunk_count, 0) = 0
                           OR coverage.min_start <> 0
                           OR coverage.max_end <> char_length(versions.canonical_text)
                        """
                    )
                    row = cursor.fetchone()
                    if row is not None and int(row["count"]) != 0:
                        errors.append("one or more versions lack complete chunk coverage")

                with connection.cursor(name="verify_documents", row_factory=dict_row) as cursor:
                    cursor.execute(
                        """
                        SELECT document_id, canonical_url
                        FROM kx.documents ORDER BY document_id
                        """
                    )
                    for row in cursor:
                        expected = document_id(str(row["canonical_url"]))
                        if expected != str(row["document_id"]):
                            errors.append(f"document id mismatch: {row['document_id']}")

                with connection.cursor(name="verify_revisions", row_factory=dict_row) as cursor:
                    cursor.execute(
                        """
                        SELECT material_id, corpus_sha256, payload, payload_sha256
                        FROM kx.source_material_revisions
                        ORDER BY corpus_sha256, material_id
                        """
                    )
                    for row in cursor:
                        payload = row["payload"]
                        if not isinstance(payload, Mapping):
                            errors.append(
                                f"material revision payload is not an object: {row['material_id']}"
                            )
                            continue
                        expected = sha256_bytes(stable_json_bytes(payload))
                        if expected != str(row["payload_sha256"]):
                            errors.append(
                                "material revision payload hash mismatch: "
                                f"{row['corpus_sha256']}:{row['material_id']}"
                            )

                with connection.cursor(name="verify_raw", row_factory=dict_row) as cursor:
                    cursor.execute(
                        """
                        SELECT raw_sha256, raw_bytes, stored_bytes, content
                        FROM kx.raw_blobs ORDER BY raw_sha256
                        """
                    )
                    for row in cursor:
                        content = bytes(row["content"])
                        if len(content) != int(row["stored_bytes"]):
                            errors.append(f"stored size mismatch: {row['raw_sha256']}")
                            continue
                        try:
                            raw = gzip.decompress(content)
                        except (OSError, EOFError):
                            errors.append(f"gzip failure: {row['raw_sha256']}")
                            continue
                        if len(raw) != int(row["raw_bytes"]):
                            errors.append(f"raw size mismatch: {row['raw_sha256']}")
                        if sha256_bytes(raw) != str(row["raw_sha256"]):
                            errors.append(f"raw hash mismatch: {row['raw_sha256']}")
                with connection.cursor(name="verify_versions", row_factory=dict_row) as cursor:
                    cursor.execute(
                        """
                        SELECT version_id, document_id, raw_sha256,
                               canonical_text, canonical_text_sha256,
                               parser_config_sha256
                        FROM kx.document_versions ORDER BY version_id
                        """
                    )
                    for row in cursor:
                        text_sha256 = sha256_bytes(str(row["canonical_text"]).encode("utf-8"))
                        if text_sha256 != str(row["canonical_text_sha256"]):
                            errors.append(f"text hash mismatch: {row['version_id']}")
                        expected = version_id(
                            document=str(row["document_id"]),
                            raw_sha256=str(row["raw_sha256"]),
                            text_sha256=text_sha256,
                            parser_config_sha256=str(row["parser_config_sha256"]),
                        )
                        if expected != str(row["version_id"]):
                            errors.append(f"version id mismatch: {row['version_id']}")
        return {
            "status": "ok" if not errors else "failed",
            "full": full,
            "errors": errors[:100],
            "errorCount": len(errors),
            "totals": status,
        }


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
