from __future__ import annotations

import gzip
import json
import shutil
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
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
    PARSER_NAME,
    PARSER_VERSION,
    chunk_text,
    document_id,
    sha256_bytes,
    stable_json_bytes,
    version_id,
)
from radar_kx.issue_perimeter import PerimeterExport
from radar_kx.manifest import Manifest
from radar_kx.parser import ParsedContent, parse_content
from radar_kx.url_policy import canonical_identity_url, normalize_url

SCHEMA_VERSION = 3
PERIMETER_PRIORITY = 100

#: Source kinds a network fetch may record. The ladder rungs that are HTTP
#: requests, and nothing else - a fetch recorded as an operator artifact is
#: defect D9, where two ordinary browser-header requests entered the evidence
#: base as material an operator had handed over.
NETWORK_SOURCE_KINDS = frozenset(
    {
        "network",
        "network_robots_override",
        "network_browser_headers",
        "browser_render",
        "web_archive",
    }
)

#: Source kinds an offline file import may record. No HTTP request happened, so
#: none of these may ever appear on a fetch result.
ARTIFACT_SOURCE_KINDS = frozenset({"operator_artifact", "local_import"})

#: Source kinds the one-shot legacy cache import may record.
CACHE_SOURCE_KINDS = frozenset({"legacy_snapshot", "legacy_truncated"})


@dataclass(frozen=True, slots=True)
class VersionProvenance:
    """How one version's bytes were actually obtained.

    Append-only beside the version, because the version id is a hash over the
    document, the raw bytes, the parser config and the text - the acquisition
    method is not part of it, so a wrong one cannot be fixed by writing a new
    version (defect D12).
    """

    source_access_method: str
    archive_used: bool = False
    archive_url: str | None = None
    archive_captured_at: datetime | None = None
    browser_used: bool = False
    manual_review_required: bool = False
    manual_review_reason: str | None = None
    provided_by: str | None = None
    provided_at: datetime | None = None
    original_url: str | None = None
    notes: str | None = None

    def comparable(self) -> tuple[object, ...]:
        """The fields that decide whether an append would say anything new."""
        return (
            self.source_access_method,
            self.archive_used,
            self.archive_url,
            self.archive_captured_at,
            self.browser_used,
            self.manual_review_required,
            self.manual_review_reason,
            self.provided_by,
            self.provided_at,
            self.original_url,
            self.notes,
        )


@dataclass(frozen=True, slots=True)
class ArtifactVersionOutcome:
    document_id: str
    version_id: str | None
    created: bool
    is_complete: bool


@dataclass(frozen=True, slots=True)
class ProvenanceOutcome:
    document_id: str
    appended: int
    unchanged: int


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
                           queue.robots_override, queue.body_limit_bytes,
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
                robots_override=bool(row["robots_override"]),
                body_limit_bytes=(
                    int(row["body_limit_bytes"]) if row["body_limit_bytes"] is not None else None
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

    @staticmethod
    def _insert_provenance(
        cursor: psycopg.Cursor[dict[str, Any]],
        *,
        version: str,
        provenance: VersionProvenance,
        recorded_by: str,
    ) -> bool:
        """Append provenance unless the latest row already says exactly this.

        The table is append-only, so re-running an import must not stack identical
        rows: the history would fill with noise and "when did this change" would
        stop being answerable.
        """
        cursor.execute(
            """
            SELECT source_access_method, archive_used, archive_url, archive_captured_at,
                   browser_used, manual_review_required, manual_review_reason,
                   provided_by, provided_at, original_url, notes
            FROM kx.version_provenance_current
            WHERE version_id = %s
            """,
            (version,),
        )
        row = cursor.fetchone()
        if row is not None and tuple(row.values()) == provenance.comparable():
            return False
        cursor.execute(
            """
            INSERT INTO kx.version_provenance (
                version_id, source_access_method, archive_used, archive_url,
                archive_captured_at, browser_used, manual_review_required,
                manual_review_reason, provided_by, provided_at, original_url,
                notes, recorded_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                version,
                provenance.source_access_method,
                provenance.archive_used,
                provenance.archive_url,
                provenance.archive_captured_at,
                provenance.browser_used,
                provenance.manual_review_required,
                provenance.manual_review_reason,
                provenance.provided_by,
                provenance.provided_at,
                provenance.original_url,
                provenance.notes,
                recorded_by,
            ),
        )
        return True

    def store_artifact_version(
        self,
        *,
        canonical_url: str,
        body: bytes,
        parsed: ParsedContent,
        source_kind: str,
        fetched_at: datetime,
        provenance: VersionProvenance,
        recorded_by: str,
    ) -> ArtifactVersionOutcome:
        """Store a document that arrived as a file, together with its provenance.

        No ``fetch_attempts`` row is written: nothing was requested over the
        network, and inventing an HTTP 200 for a file is how a browser fetch came
        to be recorded as an operator artifact in the first place.
        """
        if source_kind not in ARTIFACT_SOURCE_KINDS:
            raise ValueError(
                f"an offline import may not record source kind {source_kind!r}; "
                f"expected one of {sorted(ARTIFACT_SOURCE_KINDS)}"
            )
        identity = canonical_identity_url(canonical_url)
        identifier = document_id(identity)
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
                    (identifier, identity),
                )
                raw_sha256, _ = self._insert_raw_blob(cursor, body=body)
                text_sha256 = sha256_bytes(parsed.text.encode("utf-8"))
                expected = version_id(
                    document=identifier, raw_sha256=raw_sha256, text_sha256=text_sha256
                )
                cursor.execute(
                    "SELECT 1 FROM kx.document_versions WHERE version_id = %s", (expected,)
                )
                created = cursor.fetchone() is None
                version = self._insert_version(
                    cursor,
                    document=identifier,
                    raw_sha256=raw_sha256,
                    parsed=parsed,
                    source_kind=source_kind,
                    fetched_at=fetched_at,
                )
                if version is not None:
                    self._insert_provenance(
                        cursor,
                        version=version,
                        provenance=provenance,
                        recorded_by=recorded_by,
                    )
                if version is not None and parsed.is_complete:
                    # The queue exists to say "we still need the text". We have it.
                    cursor.execute(
                        """
                        UPDATE kx.fetch_queue
                        SET status = 'succeeded', lease_token = NULL, lease_until = NULL,
                            updated_at = clock_timestamp()
                        WHERE document_id = %s AND status <> 'succeeded'
                        """,
                        (identifier,),
                    )
            connection.commit()
        return ArtifactVersionOutcome(
            document_id=identifier,
            version_id=version,
            created=created and version is not None,
            is_complete=parsed.is_complete,
        )

    def record_version_provenance(
        self,
        *,
        canonical_url: str,
        provenance: VersionProvenance,
        recorded_by: str,
        source_kinds: frozenset[str] | None = None,
    ) -> ProvenanceOutcome | None:
        """Append provenance to every version of one document, correcting the record.

        Returns ``None`` when the document is not in the store, so a backfill can
        report what it could not find instead of failing silently.
        """
        identity = canonical_identity_url(canonical_url)
        identifier = document_id(identity)
        appended = 0
        unchanged = 0
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM kx.documents WHERE document_id = %s", (identifier,))
                if cursor.fetchone() is None:
                    return None
                if source_kinds is None:
                    cursor.execute(
                        "SELECT version_id FROM kx.document_versions WHERE document_id = %s"
                        " ORDER BY fetched_at, version_id",
                        (identifier,),
                    )
                else:
                    cursor.execute(
                        "SELECT version_id FROM kx.document_versions"
                        " WHERE document_id = %s AND source_kind = ANY(%s)"
                        " ORDER BY fetched_at, version_id",
                        (identifier, sorted(source_kinds)),
                    )
                versions = [str(row["version_id"]) for row in cursor.fetchall()]
                for version in versions:
                    if self._insert_provenance(
                        cursor,
                        version=version,
                        provenance=provenance,
                        recorded_by=recorded_by,
                    ):
                        appended += 1
                    else:
                        unchanged += 1
            connection.commit()
        return ProvenanceOutcome(document_id=identifier, appended=appended, unchanged=unchanged)

    def record_fetch_result(self, result: FetchResult) -> dict[str, Any]:
        if result.task.source_kind not in NETWORK_SOURCE_KINDS:
            # A network request may never be recorded as material an operator
            # handed over: that is defect D9, and once written the attempt row is
            # immutable and only a provenance correction can say otherwise.
            raise ValueError(
                f"a fetch may not record source kind {result.task.source_kind!r}; "
                f"expected one of {sorted(NETWORK_SOURCE_KINDS)}"
            )
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
                            source_kind=result.task.source_kind,
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
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        result.task.document_id,
                        result.task.source_kind,
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
        if source_kind not in CACHE_SOURCE_KINDS:
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

    def import_issue_perimeter(self, export: PerimeterExport) -> dict[str, Any]:
        """Record one immutable editorial snapshot of issue -> material selections."""
        source = export.source
        created_documents = 0
        queued_documents = 0
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source_sha256 FROM kx.issue_perimeter_sources
                    WHERE perimeter_source_id = %s
                    """,
                    (source.perimeter_source_id,),
                )
                existing = cursor.fetchone()
                if existing is not None and str(existing["source_sha256"]) != source.source_sha256:
                    raise RuntimeError(
                        f"perimeter source {source.perimeter_source_id} was already imported "
                        "from a different artifact"
                    )
                cursor.execute(
                    """
                    INSERT INTO kx.issue_perimeter_sources (
                        perimeter_source_id, source_kind, source_reference,
                        source_sha256, captured_at, row_count, document_count
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (perimeter_source_id) DO NOTHING
                    """,
                    (
                        source.perimeter_source_id,
                        source.source_kind,
                        source.source_reference,
                        source.source_sha256,
                        source.captured_at,
                        len(export.members),
                        len(export.document_ids),
                    ),
                )
                for member in export.members:
                    cursor.execute(
                        """
                        INSERT INTO kx.documents (document_id, canonical_url)
                        VALUES (%s, %s) ON CONFLICT (document_id) DO NOTHING
                        """,
                        (member.document_id, member.canonical_url),
                    )
                    created_documents += cursor.rowcount
                    cursor.execute(
                        """
                        INSERT INTO kx.fetch_queue (document_id, status, priority)
                        VALUES (%s, 'pending', %s) ON CONFLICT (document_id) DO NOTHING
                        """,
                        (member.document_id, PERIMETER_PRIORITY),
                    )
                    queued_documents += cursor.rowcount
                    cursor.execute(
                        """
                        INSERT INTO kx.issue_perimeter_members (
                            perimeter_source_id, issue_id, material_ref, document_id,
                            issue_date, issue_number, issue_title, sort_order,
                            perimeter, verdict, key_material, signal_score,
                            signal_strength, title, source_url, canonical_url,
                            summary, agpm_takeaway, brief, trend_notes,
                            theses, flags, published_raw, payload, payload_sha256
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (perimeter_source_id, issue_id, material_ref) DO NOTHING
                        """,
                        (
                            source.perimeter_source_id,
                            member.issue_id,
                            member.material_ref,
                            member.document_id,
                            member.issue_date,
                            member.issue_number,
                            member.issue_title,
                            member.sort_order,
                            member.perimeter,
                            member.verdict,
                            member.key_material,
                            member.signal_score,
                            member.signal_strength,
                            member.title,
                            member.source_url,
                            member.canonical_url,
                            member.summary,
                            member.agpm_takeaway,
                            member.brief,
                            member.trend_notes,
                            Jsonb(member.theses),
                            Jsonb(member.flags),
                            member.published_raw,
                            Jsonb(member.payload),
                            member.payload_sha256,
                        ),
                    )
            connection.commit()
        return {
            "perimeterSourceId": source.perimeter_source_id,
            "memberRows": len(export.members),
            "documents": len(export.document_ids),
            "createdDocuments": created_documents,
            "queuedDocuments": queued_documents,
        }

    def perimeter_status(self) -> dict[str, Any]:
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM kx.issue_perimeter_sources) AS sources,
                        (SELECT count(*) FROM kx.issue_perimeter_members) AS member_rows,
                        (SELECT count(DISTINCT issue_id)
                         FROM kx.issue_perimeter_members) AS issues,
                        (SELECT count(DISTINCT material_ref)
                         FROM kx.issue_perimeter_members) AS materials,
                        (SELECT count(*) FROM kx.issue_perimeter_documents) AS documents,
                        (SELECT count(*) FROM kx.issue_perimeter_documents
                         WHERE best_version_id IS NOT NULL) AS complete_documents,
                        (SELECT count(*) FROM kx.fetch_queue AS queue
                         JOIN kx.issue_perimeter_documents USING (document_id)
                         WHERE queue.robots_override) AS robots_override_documents
                    """
                )
                totals = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT queue.status, count(*) AS count
                    FROM kx.fetch_queue AS queue
                    JOIN kx.issue_perimeter_documents USING (document_id)
                    GROUP BY queue.status ORDER BY queue.status
                    """
                )
                queue = {str(row["status"]): int(row["count"]) for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT coalesce(queue.last_error_code, 'unknown') AS reason,
                           count(*) AS count
                    FROM kx.issue_perimeter_documents AS perimeter
                    LEFT JOIN kx.fetch_queue AS queue USING (document_id)
                    WHERE perimeter.best_version_id IS NULL
                    GROUP BY reason ORDER BY count DESC, reason
                    """
                )
                reasons = {str(row["reason"]): int(row["count"]) for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT sources.perimeter_source_id, sources.source_kind,
                           sources.source_reference, sources.source_sha256,
                           sources.row_count, sources.document_count
                    FROM kx.issue_perimeter_sources AS sources
                    ORDER BY sources.perimeter_source_id
                    """
                )
                source_rows = [dict(row) for row in cursor.fetchall()]
        if totals is None:
            raise RuntimeError("perimeter status query returned no row")
        counts = {key: int(value) for key, value in totals.items()}
        return {
            **counts,
            "incomplete_documents": counts["documents"] - counts["complete_documents"],
            "queue": queue,
            "incomplete_reasons": reasons,
            "sources": source_rows,
        }

    def iter_perimeter_gaps(self, *, limit: int = 500) -> Iterator[dict[str, Any]]:
        with self.connect() as connection, connection.cursor() as cursor:
            self.require_schema(connection)
            cursor.execute(
                """
                SELECT perimeter.canonical_url, perimeter.issue_count,
                       perimeter.first_issue_date, perimeter.last_issue_date,
                       queue.status, queue.attempt_count, queue.last_http_status,
                       queue.last_error_code, queue.last_error_detail,
                       queue.robots_override,
                       (SELECT count(*) FROM kx.document_versions AS versions
                        WHERE versions.document_id = perimeter.document_id) AS versions
                FROM kx.issue_perimeter_documents AS perimeter
                LEFT JOIN kx.fetch_queue AS queue USING (document_id)
                WHERE perimeter.best_version_id IS NULL
                ORDER BY queue.last_error_code, perimeter.canonical_url
                LIMIT %s
                """,
                (limit,),
            )
            yield from cursor.fetchall()

    def prepare_perimeter(
        self,
        *,
        robots_override_reason: str | None = None,
        body_limit_bytes: int | None = None,
        requeue: bool = False,
    ) -> dict[str, Any]:
        """Apply audited per-document policy to issue-perimeter documents without full text.

        The scope is deliberately narrow: only documents that a published Radar issue
        already selected and that still have no complete canonical version.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE kx.fetch_queue AS queue
                    SET priority = %s, updated_at = clock_timestamp()
                    FROM kx.issue_perimeter_documents AS perimeter
                    WHERE queue.document_id = perimeter.document_id
                      AND queue.priority < %s
                    """,
                    (PERIMETER_PRIORITY, PERIMETER_PRIORITY),
                )
                prioritized = cursor.rowcount
                overridden = 0
                if robots_override_reason:
                    cursor.execute(
                        """
                        UPDATE kx.fetch_queue AS queue
                        SET robots_override = true,
                            robots_override_reason = %s,
                            updated_at = clock_timestamp()
                        FROM kx.issue_perimeter_documents AS perimeter
                        WHERE queue.document_id = perimeter.document_id
                          AND perimeter.best_version_id IS NULL
                          AND NOT queue.robots_override
                        """,
                        (robots_override_reason,),
                    )
                    overridden = cursor.rowcount
                relaxed = 0
                if body_limit_bytes is not None:
                    cursor.execute(
                        """
                        UPDATE kx.fetch_queue AS queue
                        SET body_limit_bytes = %s, updated_at = clock_timestamp()
                        FROM kx.issue_perimeter_documents AS perimeter
                        WHERE queue.document_id = perimeter.document_id
                          AND perimeter.best_version_id IS NULL
                          AND queue.body_limit_bytes IS DISTINCT FROM %s
                        """,
                        (body_limit_bytes, body_limit_bytes),
                    )
                    relaxed = cursor.rowcount
                requeued = 0
                if requeue:
                    cursor.execute(
                        """
                        UPDATE kx.fetch_queue AS queue
                        SET status = 'retry', attempt_count = 0,
                            next_attempt_at = clock_timestamp(),
                            lease_token = NULL, lease_until = NULL,
                            updated_at = clock_timestamp()
                        FROM kx.issue_perimeter_documents AS perimeter
                        WHERE queue.document_id = perimeter.document_id
                          AND perimeter.best_version_id IS NULL
                          AND queue.status = 'failed'
                        """
                    )
                    requeued = cursor.rowcount
            connection.commit()
        return {
            "prioritized": prioritized,
            "robotsOverridden": overridden,
            "bodyLimitRelaxed": relaxed,
            "requeued": requeued,
        }

    def reparse_perimeter_gaps(self, *, reason: str, min_text_chars: int) -> dict[str, Any]:
        """Re-parse retained raw evidence for perimeter documents that still lack full text.

        No network request is made. Truncated legacy caches are excluded, because a
        20,000-character excerpt must never be relabelled as a complete article.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (attempts.document_id, attempts.raw_sha256)
                           attempts.document_id, attempts.raw_sha256, attempts.source_kind,
                           attempts.content_type, attempts.final_url, attempts.finished_at,
                           perimeter.canonical_url
                    FROM kx.issue_perimeter_documents AS perimeter
                    JOIN kx.fetch_attempts AS attempts
                      ON attempts.document_id = perimeter.document_id
                    WHERE perimeter.best_version_id IS NULL
                      AND attempts.raw_sha256 IS NOT NULL
                      AND attempts.source_kind <> 'legacy_truncated'
                    ORDER BY attempts.document_id, attempts.raw_sha256,
                             attempts.finished_at DESC
                    """
                )
                candidates = cursor.fetchall()
            completed: list[str] = []
            versions = 0
            for candidate in candidates:
                started_at = datetime.now(UTC)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT content FROM kx.raw_blobs WHERE raw_sha256 = %s",
                        (candidate["raw_sha256"],),
                    )
                    blob = cursor.fetchone()
                if blob is None:
                    continue
                body = gzip.decompress(bytes(blob["content"]))
                try:
                    parsed = parse_content(
                        body=body,
                        content_type=str(candidate["content_type"] or ""),
                        source_url=str(candidate["final_url"] or candidate["canonical_url"]),
                        min_text_chars=min_text_chars,
                    )
                    outcome = "complete" if parsed.is_complete else f"incomplete:{parsed.quality}"
                except Exception as exc:
                    # Parser libraries operate on untrusted retained bodies. Isolate the
                    # document instead of aborting the whole derived re-parse pass.
                    parsed = None
                    outcome = f"parse_error:{type(exc).__name__}"
                version: str | None = None
                with connection.transaction(), connection.cursor() as cursor:
                    if parsed is not None and parsed.is_complete:
                        version = self._insert_version(
                            cursor,
                            document=str(candidate["document_id"]),
                            raw_sha256=str(candidate["raw_sha256"]),
                            parsed=parsed,
                            source_kind=str(candidate["source_kind"]),
                            fetched_at=candidate["finished_at"],
                        )
                        cursor.execute(
                            """
                            UPDATE kx.fetch_queue
                            SET status = 'succeeded', last_error_code = NULL,
                                last_error_detail = 'completed by derived reparse',
                                next_attempt_at = clock_timestamp(),
                                updated_at = clock_timestamp()
                            WHERE document_id = %s AND status <> 'succeeded'
                            """,
                            (candidate["document_id"],),
                        )
                        completed.append(str(candidate["canonical_url"]))
                        versions += 1
                    cursor.execute(
                        """
                        INSERT INTO kx.reparse_runs (
                            document_id, raw_sha256, version_id, parser_name,
                            parser_version, parser_config_sha256, reason, outcome,
                            worker_release, started_at, finished_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            candidate["document_id"],
                            candidate["raw_sha256"],
                            version,
                            PARSER_NAME,
                            PARSER_VERSION,
                            PARSER_CONFIG_HASH,
                            reason,
                            outcome,
                            self.settings.release_id,
                            started_at,
                            datetime.now(UTC),
                        ),
                    )
                connection.commit()
        return {
            "candidates": len(candidates),
            "versions": versions,
            "completedDocuments": sorted(set(completed)),
        }

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
                        SELECT count(*) AS count
                        FROM (
                            SELECT sources.perimeter_source_id
                            FROM kx.issue_perimeter_sources AS sources
                            LEFT JOIN kx.issue_perimeter_members AS members
                              USING (perimeter_source_id)
                            GROUP BY sources.perimeter_source_id, sources.row_count,
                                     sources.document_count
                            HAVING count(members.material_ref) <> sources.row_count
                                OR count(DISTINCT members.document_id)
                                   <> sources.document_count
                        ) AS mismatches
                        """
                    )
                    row = cursor.fetchone()
                    if row is not None and int(row["count"]) != 0:
                        errors.append("issue perimeter source counts do not match members")

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
