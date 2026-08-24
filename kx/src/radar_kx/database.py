from __future__ import annotations

import gzip
import json
import shutil
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, ClassVar, cast

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from radar_kx.acquisition import ESCALATION_HINT, HostProfile, next_step, profile_for
from radar_kx.config import Settings
from radar_kx.dates import ResolvedDate
from radar_kx.dates import resolve as resolve_date
from radar_kx.duplicates import DocumentText, DuplicateProposal
from radar_kx.embeddings import (
    DEFAULT_DIMENSIONS,
    DEFAULT_MODEL,
    encode,
    load_model,
    text_fingerprint,
    to_pgvector,
)
from radar_kx.extraction import (
    EXTRACTOR_VERSION,
    MAX_CLAIMS_PER_FRAGMENT,
    MIN_QUOTE_CHARS,
    AlignedClaim,
    Fragment,
    normalized_claim_text,
)
from radar_kx.fetcher import DocumentTask, FetchResult
from radar_kx.graph import Graph, dangling, unsupported
from radar_kx.graph import build as build_graph
from radar_kx.ideas import (
    CandidateGroup,
    ClaimRecord,
    IndependenceVerdict,
    group_claims,
    term_coverage,
)
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
from radar_kx.language import language_of
from radar_kx.linking import MAX_DISTANCE as LINK_MAX_DISTANCE
from radar_kx.linking import NEIGHBOURS as LINK_NEIGHBOURS
from radar_kx.linking import Judgement, Pair
from radar_kx.manifest import Manifest
from radar_kx.parser import ParsedContent, parse_content
from radar_kx.publication import InvariantReport, decide
from radar_kx.reading import ReadableClaim, Reading, not_in_the_backbone
from radar_kx.reading import valid_until as expiry_of
from radar_kx.reconciliation import FileStoreEntry, compare, payload_sha256
from radar_kx.release import ReleaseComposition, ReleaseError, compose, reconcile
from radar_kx.research import (
    EvidenceElement,
    Refusal,
    Verification,
    build_package,
    normalize_question,
)
from radar_kx.search import (
    AGENT_FILTERS,
    AGENT_SEARCH_SQL,
    FILTERS,
    RRF_K,
    SCOPES,
    SEMANTIC_DEPTH,
    SMOKE_QUERIES,
    SearchHit,
    build_hit,
    evidence_sql,
    search_sql,
)
from radar_kx.skeleton import AuthoredSkeleton, SkeletonCandidate, SkeletonError
from radar_kx.skeleton import candidates as skeleton_candidates
from radar_kx.source_families import DocumentHost, FamilyDecision, propose_families
from radar_kx.spans import Repair
from radar_kx.spans import expand as expand_span
from radar_kx.topics import LEDE_CHARS, AssignableItem, Assignment, document_item
from radar_kx.url_policy import canonical_identity_url, normalize_url
from radar_kx.wiki_import import (
    DEFAULT_RELEVANCE_FLOOR,
    EVIDENCE_SEARCH_SQL,
    ParsedPage,
    is_authored,
    parse_page,
)
from radar_kx.wiki_snapshot import WikiSnapshot, compress

#: The schema version the deployed worker requires. It is bumped **when a
#: migration is applied to production**, not when it is written: `require_schema`
#: is a hard gate (defect D2), so a repository that runs ahead of the database
#: cannot be released at all.
#: How many independent supporting observations the machine wants before it
#: proposes moving a statement up a rung (decision 6). Two *other* sources, not
#: two other articles: four outlets repeating one Gartner forecast are one
#: observation, and the reading pass is what makes that countable.
PROMOTION_FLOOR = 2

#: How many events of each class the observatory shows. Per class, because the
#: whole point of decision 4 is that a reader sees the classes side by side.
OBSERVATORY_PER_CLASS = 60

SCHEMA_VERSION = 28

#: Where a scan reads its documents from. One vocabulary, shared with search, so
#: the canon cannot quietly fall out of one pipeline and not another: extraction
#: first ran with a smaller two-name vocabulary, the canon was not in it, and the
#: wiki's statements about the White Paper had nothing to bind to.
#:
#: "perimeter" is an alias for the historical issue perimeter, kept because the
#: acquisition and family scans read better with it.
_SCOPE_SOURCES = dict(SCOPES)
_SCOPE_SOURCES["perimeter"] = SCOPES["historical"]

#: The scopes a caller may name. Derived from the mapping so the CLI's choices
#: and the queries cannot disagree.
SCAN_SCOPES = tuple(sorted(_SCOPE_SOURCES))

#: How a pair's measure is spelled in ``duplicate_evidence.evidence_kind``. Both
#: numbers go into ``detail`` whichever fired, so a later review never has to
#: recompute the other one to understand why a cluster exists.
_EVIDENCE_KIND = {
    "canonical_text_hash": "canonical_text_hash",
    "jaccard": "shingle_overlap",
    "containment": "shingle_containment",
}
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
class CorpusMember:
    """One member of a corpus whose documents are files rather than URLs."""

    material_id: str
    document_id: str
    canonical_url: str
    title: str
    seen_at: datetime
    payload: dict[str, Any]


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


def one_row(cursor: psycopg.Cursor[dict[str, Any]]) -> dict[str, Any]:
    """The single row an aggregate query always returns."""
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("aggregate query returned no row")
    return row


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

    def register_corpus_members(
        self,
        *,
        corpus_sha256: str,
        source_name: str,
        source_kind: str,
        members: Sequence[CorpusMember],
    ) -> dict[str, int | str]:
        """Register a corpus whose documents are files, not URLs to fetch.

        Same shape as ``import_manifest`` - a ``corpus_imports`` row with its
        immutable revisions, so ``verify --full`` can reconcile the counts - minus
        the ``fetch_queue`` row. A canon document has no web address; queueing one
        would hand the fetcher a URL it cannot parse and it would fail forever.
        """
        if source_kind == "radar_materials":
            raise ValueError("radar materials are imported by import_manifest, not here")
        document_ids = {member.document_id for member in members}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.corpus_imports (
                        corpus_sha256, source_name, row_count, document_count, source_kind
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (corpus_sha256) DO NOTHING
                    """,
                    (corpus_sha256, source_name, len(members), len(document_ids), source_kind),
                )
                for member in members:
                    cursor.execute(
                        """
                        INSERT INTO kx.documents (
                            document_id, canonical_url, first_seen_at, last_seen_at
                        ) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (document_id) DO UPDATE SET
                            last_seen_at = GREATEST(
                                kx.documents.last_seen_at, EXCLUDED.last_seen_at
                            ),
                            updated_at = clock_timestamp()
                        """,
                        (
                            member.document_id,
                            member.canonical_url,
                            member.seen_at,
                            member.seen_at,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO kx.source_materials (
                            material_id, source_url, canonical_url, title, summary,
                            raw_excerpt, perimeter, published_raw, first_seen_at,
                            last_seen_at, payload, payload_sha256, corpus_sha256
                        ) VALUES (%s, %s, %s, %s, '', '', NULL, NULL, %s, %s, %s, %s, %s)
                        ON CONFLICT (material_id) DO UPDATE SET
                            title = EXCLUDED.title,
                            last_seen_at = EXCLUDED.last_seen_at,
                            payload = EXCLUDED.payload,
                            payload_sha256 = EXCLUDED.payload_sha256,
                            corpus_sha256 = EXCLUDED.corpus_sha256,
                            updated_at = clock_timestamp()
                        """,
                        (
                            member.material_id,
                            member.canonical_url,
                            member.canonical_url,
                            member.title,
                            member.seen_at,
                            member.seen_at,
                            Jsonb(member.payload),
                            sha256_bytes(stable_json_bytes(member.payload)),
                            corpus_sha256,
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
                            member.material_id,
                            corpus_sha256,
                            member.document_id,
                            member.canonical_url,
                            member.canonical_url,
                            Jsonb(member.payload),
                            sha256_bytes(stable_json_bytes(member.payload)),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO kx.material_documents (material_id, document_id)
                        VALUES (%s, %s)
                        ON CONFLICT (material_id) DO UPDATE SET
                            document_id = EXCLUDED.document_id
                        """,
                        (member.material_id, member.document_id),
                    )
            connection.commit()
        return {
            "corpusSha256": corpus_sha256,
            "sourceKind": source_kind,
            "materials": len(members),
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

    def search(
        self,
        query: str,
        *,
        scope: str,
        limit: int,
        match: str = "all",
        question_vector: str | None = None,
    ) -> list[SearchHit]:
        """Lexical search over one membership class, fused across ru and en.

        The snippet offsets each hit reports are checked against the stored
        canonical text before the hit is returned: an offset that does not
        reproduce its own snippet is a lie the evidence layer would then build on.
        """
        statement = search_sql(scope, match=match)
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    statement,
                    {
                        "query": query,
                        "limit": limit,
                        "rrf_k": RRF_K,
                        "question_vector": question_vector,
                        "embedding_model": DEFAULT_MODEL,
                        "semantic_depth": SEMANTIC_DEPTH,
                    },
                )
                rows = cursor.fetchall()
                hits = [build_hit(row) for row in rows]
                self._assert_offsets_reproduce_snippets(cursor, hits)
        return hits

    @staticmethod
    def _assert_offsets_reproduce_snippets(
        cursor: psycopg.Cursor[dict[str, Any]], hits: Sequence[SearchHit]
    ) -> None:
        if not hits:
            return
        cursor.execute(
            """
            SELECT version_id, char_start, char_end, expected,
                   substr(canonical_text, char_start + 1, char_end - char_start) AS actual
            FROM unnest(%s::text[], %s::int[], %s::int[], %s::text[])
                 AS spans(version_id, char_start, char_end, expected)
            JOIN kx.document_versions USING (version_id)
            """,
            (
                [hit.version_id for hit in hits],
                [hit.char_start for hit in hits],
                [hit.char_end for hit in hits],
                [hit.snippet for hit in hits],
            ),
        )
        for row in cursor.fetchall():
            if row["actual"] != row["expected"]:
                raise RuntimeError(
                    "search snippet does not match its own offsets in version "
                    f"{row['version_id']} [{row['char_start']}, {row['char_end']})"
                )

    def record_store_reconciliation(
        self,
        scope: str,
        entries: Sequence[FileStoreEntry],
        *,
        source: Mapping[str, Any],
        generated_by: str,
    ) -> dict[str, Any]:
        """Compare a file-store inventory with KX and record the difference.

        The scope decides what KX side to compare against: the full-text cache is
        about documents that have text, the discovery registry is about documents
        existing at all.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT documents.document_id,
                           documents.canonical_url,
                           EXISTS (
                               SELECT 1 FROM kx.document_versions AS versions
                               WHERE versions.document_id = documents.document_id
                                 AND versions.is_complete
                           ) AS has_complete_version
                    FROM kx.documents
                    """
                )
                known = {
                    str(row["document_id"]): {
                        "canonicalUrl": row["canonical_url"],
                        "hasCompleteVersion": bool(row["has_complete_version"]),
                    }
                    for row in cursor.fetchall()
                }
            # Every document goes into the lookup, including the ones nothing could
            # fetch: a document KX knows and holds no text for is the case worth
            # reporting, and dropping it here reclassifies it as absent. Which
            # direction is worth reporting is the scope's business, not this query's.
            result = compare(scope, entries, known, source=source)
            payload = result.payload()
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.store_reconciliation_reports (
                        scope, file_store_count, kx_count, only_in_file_store,
                        only_in_kx, differing, payload, payload_sha256, generated_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING report_id, generated_at
                    """,
                    (
                        result.scope,
                        result.file_store_count,
                        result.kx_count,
                        len(result.only_in_file_store),
                        len(result.only_in_kx),
                        len(result.differing),
                        Jsonb(payload),
                        payload_sha256(payload),
                        generated_by,
                    ),
                )
                row = one_row(cursor)
            connection.commit()
        return {
            **result.as_json(),
            "reportId": row["report_id"],
            "generatedAt": row["generated_at"],
        }

    def record_egress(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        payload_chars: int,
        payload_sha256: str,
        outcome: str,
        prompt_sha256: str | None = None,
        error_detail: str | None = None,
        run_id: str | None = None,
        document_id: str | None = None,
        version_id: str | None = None,
        chunk_id: str | None = None,
        request_tokens: int | None = None,
        response_tokens: int | None = None,
    ) -> int:
        """Record one attempt to send something to a model, and return its id.

        Written for refusals as well as for calls: ``egress_audit`` is the record of
        what was attempted at the boundary (P18), and a refusal that leaves no row
        cannot be distinguished later from a call nobody made.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.egress_audit (
                        provider, model, purpose, run_id, document_id, version_id, chunk_id,
                        payload_chars, payload_sha256, prompt_sha256,
                        request_tokens, response_tokens, outcome, error_detail, worker_release
                    ) VALUES (
                        %(provider)s, %(model)s, %(purpose)s, %(run_id)s, %(document_id)s,
                        %(version_id)s, %(chunk_id)s, %(payload_chars)s, %(payload_sha256)s,
                        %(prompt_sha256)s, %(request_tokens)s, %(response_tokens)s,
                        %(outcome)s, %(error_detail)s, %(worker_release)s
                    )
                    RETURNING egress_id
                    """,
                    {
                        "provider": provider,
                        "model": model,
                        "purpose": purpose,
                        "run_id": run_id,
                        "document_id": document_id,
                        "version_id": version_id,
                        "chunk_id": chunk_id,
                        "payload_chars": payload_chars,
                        "payload_sha256": payload_sha256,
                        "prompt_sha256": prompt_sha256,
                        "request_tokens": request_tokens,
                        "response_tokens": response_tokens,
                        "outcome": outcome,
                        "error_detail": error_detail,
                        "worker_release": self.settings.release_id,
                    },
                )
                return int(one_row(cursor)["egress_id"])

    # ---------------------------------------------------------------------
    # Source independence (slice 2.4, ADR-0007)
    # ---------------------------------------------------------------------

    def documents_for_family_proposal(self, *, scope: str) -> list[DocumentHost]:
        """Every document in scope, as a document id and the URL it came from."""
        source = _SCOPE_SOURCES[scope]
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                query = (
                    f"SELECT documents.document_id, documents.canonical_url"  # noqa: S608 - a constant from _SCOPE_SOURCES
                    f" FROM ({source}) AS scoped"
                    f" JOIN kx.documents AS documents USING (document_id)"
                )
                cursor.execute(query)
                return [
                    DocumentHost(
                        document_id=str(row["document_id"]),
                        canonical_url=str(row["canonical_url"]),
                    )
                    for row in cursor.fetchall()
                ]

    def apply_family_batch(
        self, *, decided_by: str, decisions: Sequence[FamilyDecision]
    ) -> dict[str, Any]:
        """Record one weekly batch of family decisions (ADR-0007 §11a).

        Everything here is append-only. A family that already exists is not
        edited: a new decision row is written and, for anything but a retirement,
        a fresh assignment row per member. What "the family is now" is a view over
        the latest rows, so a correction next month cannot change what a score
        meant last month.
        """
        batch_id = str(uuid.uuid4())
        applied: dict[str, Any] = {
            "batchId": batch_id,
            "families": 0,
            "assignments": 0,
            "retired": 0,
        }
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for decision in decisions:
                    cursor.execute(
                        """
                        INSERT INTO kx.source_families
                            (family_key, display_name, family_kind, created_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (family_key) DO UPDATE SET family_key = EXCLUDED.family_key
                        RETURNING family_id
                        """,
                        (
                            decision.family_key,
                            decision.display_name,
                            decision.family_kind,
                            decided_by,
                        ),
                    )
                    family_id = one_row(cursor)["family_id"]
                    cursor.execute(
                        """
                        INSERT INTO kx.source_family_decisions
                            (family_id, batch_id, action, decided_by, rationale, members_sha256)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING decision_id
                        """,
                        (
                            family_id,
                            batch_id,
                            decision.action,
                            decided_by,
                            decision.rationale,
                            decision.members_sha256,
                        ),
                    )
                    decision_id = one_row(cursor)["decision_id"]
                    applied["families"] = int(applied["families"]) + 1
                    if decision.action == "retired":
                        applied["retired"] = int(applied["retired"]) + 1
                        continue
                    for document_id in decision.document_ids:
                        cursor.execute(
                            """
                            INSERT INTO kx.document_source_family
                                (document_id, family_id, decision_id)
                            VALUES (%s, %s, %s)
                            """,
                            (document_id, family_id, decision_id),
                        )
                        applied["assignments"] = int(applied["assignments"]) + 1
        return applied

    def documents_for_duplicate_scan(self, *, scope: str) -> list[DocumentText]:
        """Documents in scope that have a complete best version, with their text."""
        source = _SCOPE_SOURCES[scope]
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT documents.document_id,
                           documents.canonical_url,
                           versions.canonical_text_sha256,
                           versions.canonical_text
                    FROM ({source}) AS scoped
                    JOIN kx.documents AS documents USING (document_id)
                    JOIN kx.document_versions AS versions
                      ON versions.version_id = documents.best_version_id
                    WHERE versions.is_complete
                    """  # noqa: S608 - same constant
                )
                return [
                    DocumentText(
                        document_id=str(row["document_id"]),
                        canonical_url=str(row["canonical_url"]),
                        text_sha256=str(row["canonical_text_sha256"]),
                        text=str(row["canonical_text"]),
                    )
                    for row in cursor.fetchall()
                ]

    def record_duplicate_proposals(
        self, proposals: Sequence[DuplicateProposal], *, proposed_by: str
    ) -> dict[str, Any]:
        """Store proposed clusters and their evidence. Nothing is confirmed here."""
        batch_id = str(uuid.uuid4())
        recorded: dict[str, Any] = {
            "batchId": batch_id,
            "clusters": 0,
            "members": 0,
            "evidence": 0,
        }
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for proposal in proposals:
                    cursor.execute(
                        """
                        INSERT INTO kx.content_duplicate_clusters
                            (cluster_kind, formation_method, shingle_threshold,
                             shingle_width, shingle_measure, proposed_by, batch_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING cluster_id
                        """,
                        (
                            proposal.cluster_kind,
                            proposal.formation_method,
                            proposal.shingle_threshold,
                            proposal.shingle_width,
                            proposal.shingle_measure,
                            proposed_by,
                            batch_id,
                        ),
                    )
                    cluster_id = one_row(cursor)["cluster_id"]
                    recorded["clusters"] = int(recorded["clusters"]) + 1
                    for document_id in proposal.document_ids:
                        cursor.execute(
                            "INSERT INTO kx.content_duplicate_cluster_members"
                            " (cluster_id, document_id) VALUES (%s, %s)",
                            (cluster_id, document_id),
                        )
                        recorded["members"] = int(recorded["members"]) + 1
                    for pair in proposal.pairs:
                        cursor.execute(
                            """
                            INSERT INTO kx.duplicate_evidence
                                (cluster_id, evidence_kind, left_document_id,
                                 right_document_id, similarity, detail, recorded_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                cluster_id,
                                _EVIDENCE_KIND[pair.measure],
                                pair.left,
                                pair.right,
                                pair.similarity,
                                Jsonb(
                                    {
                                        "jaccard": round(pair.jaccard, 4),
                                        "containment": round(pair.containment, 4),
                                    }
                                ),
                                proposed_by,
                            ),
                        )
                        recorded["evidence"] = int(recorded["evidence"]) + 1
        return recorded

    def confirm_duplicate_clusters(self, *, batch_id: str, confirmed_by: str) -> int:
        """Confirm every cluster of one batch. Only confirmed clusters collapse a count."""
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE kx.content_duplicate_clusters
                    SET confirmed_at = clock_timestamp(), confirmed_by = %s
                    WHERE batch_id = %s AND confirmed_at IS NULL
                    """,
                    (confirmed_by, batch_id),
                )
                return cursor.rowcount

    def independence_report(self, document_ids: Sequence[str]) -> dict[str, Any]:
        """Apply the counting rules of ADR-0007 §2 to a set of documents."""
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM kx.independence_report(%s::char(64)[])",
                    (list(document_ids),),
                )
                row = one_row(cursor)
        return {
            "documentsConsidered": int(row["documents_considered"]),
            "independentSources": int(row["independent_sources"]),
            "unknownDocuments": int(row["unknown_documents"]),
            "collapsedByFamily": int(row["collapsed_by_family"]),
            "collapsedByCluster": int(row["collapsed_by_cluster"]),
        }

    # ---------------------------------------------------------------------
    # Extraction (slice 2.6, plan §10.2 and §11.3)
    # ---------------------------------------------------------------------

    def extraction_fragments(self, *, scope: str, limit: int) -> list[Fragment]:
        """Chunks in scope that no successful extraction run has covered yet."""
        source = _SCOPE_SOURCES[scope]
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT chunks.chunk_id,
                           chunks.version_id,
                           chunks.char_start,
                           chunks.char_end,
                           chunks.text
                    FROM ({source}) AS scoped
                    JOIN kx.documents AS documents USING (document_id)
                    JOIN kx.chunks AS chunks ON chunks.version_id = documents.best_version_id
                    JOIN kx.document_versions AS versions
                      ON versions.version_id = chunks.version_id
                    WHERE versions.is_complete
                      AND NOT EXISTS (
                          SELECT 1 FROM kx.processing_runs AS runs
                          WHERE runs.version_id = chunks.version_id
                            AND runs.processor = 'claim_extraction'
                            AND runs.status = 'succeeded'
                            AND runs.raw_output ->> 'chunkId' = chunks.chunk_id
                      )
                    ORDER BY chunks.version_id, chunks.ordinal
                    LIMIT %s
                    """,  # noqa: S608 - a constant from _SCOPE_SOURCES
                    (limit,),
                )
                return [
                    Fragment(
                        version_id=str(row["version_id"]),
                        chunk_id=str(row["chunk_id"]),
                        char_start=int(row["char_start"]),
                        char_end=int(row["char_end"]),
                        text=str(row["text"]),
                    )
                    for row in cursor.fetchall()
                ]

    @staticmethod
    def extraction_parameters(fragment: Fragment) -> dict[str, Any]:
        """What identifies this run, including which fragment it read.

        The fragment belongs in the parameters because ``processing_runs`` is
        unique on them: without it, the second chunk of a document would collide
        with the first and a whole document would get one run.
        """
        return {
            "extractor": EXTRACTOR_VERSION,
            "chunkId": fragment.chunk_id,
            "charStart": fragment.char_start,
            "charEnd": fragment.char_end,
            "minQuoteChars": MIN_QUOTE_CHARS,
            "maxClaims": MAX_CLAIMS_PER_FRAGMENT,
        }

    def canonical_text(self, version_id: str) -> str:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT canonical_text FROM kx.document_versions WHERE version_id = %s",
                (version_id,),
            )
            return str(one_row(cursor)["canonical_text"])

    def record_extraction(
        self,
        fragment: Fragment,
        aligned: Sequence[AlignedClaim],
        *,
        model: str,
        prompt_sha256: str,
        failure: str | None = None,
    ) -> dict[str, Any]:
        """Write one extraction run: exact spans as evidence, the rest as candidates.

        Idempotent on the recipe. ``processing_runs`` is unique on
        ``(version_id, processor, processor_version, parameters_sha256, model_id)``
        and the parameters name the fragment, so re-running the same fragment with
        the same recipe records nothing twice and says so.
        """
        parameters = self.extraction_parameters(fragment)
        outcome: dict[str, Any] = {
            "chunkId": fragment.chunk_id,
            "claims": 0,
            "candidates": 0,
            "byReason": {},
        }
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.processing_runs
                        (version_id, processor, processor_version, parameters_sha256,
                         model_id, status, prompt_sha256, raw_output)
                    VALUES (%s, 'claim_extraction', %s, %s, %s, 'running', %s, %s)
                    ON CONFLICT (version_id, processor, processor_version,
                                 parameters_sha256, model_id) DO NOTHING
                    RETURNING run_id
                    """,
                    (
                        fragment.version_id,
                        EXTRACTOR_VERSION,
                        sha256_bytes(stable_json_bytes(parameters)),
                        model,
                        prompt_sha256,
                        Jsonb({"chunkId": fragment.chunk_id}),
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    # The recipe already has a run for this fragment. If it
                    # succeeded, there is nothing to do. If it failed, the row is
                    # occupying the unique key and must be retried rather than left
                    # to block the fragment forever (migration 009).
                    cursor.execute(
                        "SELECT run_id, status, attempt_count FROM kx.processing_runs"
                        " WHERE version_id = %s AND processor = 'claim_extraction'"
                        "   AND processor_version = %s AND parameters_sha256 = %s"
                        "   AND model_id = %s",
                        (
                            fragment.version_id,
                            EXTRACTOR_VERSION,
                            sha256_bytes(stable_json_bytes(parameters)),
                            model,
                        ),
                    )
                    previous = one_row(cursor)
                    if str(previous["status"]) == "succeeded":
                        outcome["skipped"] = "this fragment already has a run of this recipe"
                        return outcome
                    run_id = previous["run_id"]
                    outcome["retriedAttempt"] = int(cast(int, previous["attempt_count"])) + 1
                    cursor.execute(
                        "UPDATE kx.processing_runs SET status = 'running',"
                        " attempt_count = attempt_count + 1, error_detail = NULL,"
                        " finished_at = NULL, prompt_sha256 = %s WHERE run_id = %s",
                        (prompt_sha256, run_id),
                    )
                    # What the failed attempt left behind is not a finding about the
                    # document; it is a record of an attempt that did not happen.
                    cursor.execute(
                        "UPDATE kx.extraction_candidates"
                        " SET status = 'discarded', resolved_at = clock_timestamp(),"
                        "     resolved_by = 'retry'"
                        " WHERE run_id = %s AND status = 'open'",
                        (run_id,),
                    )
                    outcome["discardedFromPreviousAttempt"] = cursor.rowcount
                else:
                    run_id = row["run_id"]
                outcome["runId"] = str(run_id)

                if failure is not None:
                    self._record_candidate_row(
                        cursor,
                        fragment,
                        run_id,
                        predicate="(none)",
                        object_text="(none)",
                        quote=failure[:2000],
                        reason="malformed_output",
                        detail=failure[:2000],
                        model=model,
                        prompt_sha=prompt_sha256,
                    )
                    outcome["candidates"] = 1
                    outcome["byReason"] = {"malformed_output": 1}
                    cursor.execute(
                        "UPDATE kx.processing_runs SET status = 'failed',"
                        " finished_at = clock_timestamp(), error_detail = %s WHERE run_id = %s",
                        (failure[:2000], run_id),
                    )
                    return outcome

                reasons: dict[str, int] = {}
                for item in aligned:
                    if item.alignment.is_exact:
                        self._record_claim_row(cursor, fragment, run_id, item)
                        outcome["claims"] = int(outcome["claims"]) + 1
                        continue
                    reason = item.alignment.reason or "malformed_output"
                    self._record_candidate_row(
                        cursor,
                        fragment,
                        run_id,
                        predicate=item.proposed.predicate,
                        object_text=item.proposed.object_text,
                        quote=item.proposed.quote,
                        reason=reason,
                        detail=item.alignment.detail,
                        model=model,
                        prompt_sha=prompt_sha256,
                    )
                    reasons[reason] = reasons.get(reason, 0) + 1
                outcome["candidates"] = sum(reasons.values())
                outcome["byReason"] = reasons
                cursor.execute(
                    "UPDATE kx.processing_runs SET status = 'succeeded',"
                    " finished_at = clock_timestamp() WHERE run_id = %s",
                    (run_id,),
                )
        return outcome

    @staticmethod
    def _record_claim_row(
        cursor: psycopg.Cursor[dict[str, Any]],
        fragment: Fragment,
        run_id: Any,
        item: AlignedClaim,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO kx.claims
                (version_id, processing_run_id, claim_kind, predicate,
                 object_text, normalized_text)
            VALUES (%s, %s, 'asserted', %s, %s, %s)
            RETURNING claim_id
            """,
            (
                fragment.version_id,
                run_id,
                item.proposed.predicate,
                item.proposed.object_text,
                normalized_claim_text(item.proposed.predicate, item.proposed.object_text),
            ),
        )
        claim_id = one_row(cursor)["claim_id"]
        # The quotation stored here came out of the store, not out of the answer.
        # That is what makes the exactness structural rather than hopeful.
        quote = item.alignment.quote_text or ""
        cursor.execute(
            """
            INSERT INTO kx.claim_evidence
                (claim_id, version_id, char_start, char_end, quote_text,
                 quote_sha256, match_status)
            VALUES (%s, %s, %s, %s, %s, %s, 'exact')
            """,
            (
                claim_id,
                fragment.version_id,
                item.alignment.char_start,
                item.alignment.char_end,
                quote,
                sha256_bytes(quote.encode("utf-8")),
            ),
        )

    @staticmethod
    def _record_candidate_row(
        cursor: psycopg.Cursor[dict[str, Any]],
        fragment: Fragment,
        run_id: Any,
        *,
        predicate: str,
        object_text: str,
        quote: str,
        reason: str,
        detail: str | None,
        model: str,
        prompt_sha: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO kx.extraction_candidates
                (version_id, chunk_id, run_id, predicate, object_text, proposed_quote,
                 proposed_quote_sha256, reason, reason_detail, model, prompt_sha256,
                 extractor_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                fragment.version_id,
                fragment.chunk_id,
                run_id,
                predicate,
                object_text,
                quote,
                sha256_bytes(quote.encode("utf-8")),
                reason,
                detail,
                model,
                prompt_sha,
                EXTRACTOR_VERSION,
            ),
        )

    def extraction_report(self) -> dict[str, Any]:
        """What extraction has produced, and where it is losing quotations."""
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                           count(*) FILTER (WHERE status = 'failed') AS failed,
                           count(*) FILTER (WHERE status = 'running') AS running
                    FROM kx.processing_runs WHERE processor = 'claim_extraction'
                    """
                )
                runs = dict(one_row(cursor))
                cursor.execute(
                    "SELECT count(*) AS claims,"
                    " count(DISTINCT version_id) AS versions FROM kx.claims"
                )
                claims = dict(one_row(cursor))
                cursor.execute(
                    "SELECT reason, count(*) AS total FROM kx.extraction_candidates"
                    " WHERE status = 'open' GROUP BY reason ORDER BY total DESC"
                )
                reasons = {str(row["reason"]): int(row["total"]) for row in cursor.fetchall()}
        total_claims = int(claims["claims"])
        total_candidates = sum(reasons.values())
        attempted = total_claims + total_candidates
        return {
            "runs": {key: int(value) for key, value in runs.items()},
            "claims": total_claims,
            "versionsWithClaims": int(claims["versions"]),
            "openCandidates": total_candidates,
            "candidatesByReason": reasons,
            # The number that says whether the recipe works: of everything the model
            # proposed, what share could be pinned to an exact span.
            "exactShare": round(total_claims / attempted, 4) if attempted else None,
        }

    def language_drift(self, *, limit: int = 20) -> dict[str, Any]:
        """How the stored language labels differ from what the detector says now.

        Nothing is rewritten. A version's label was produced by the parser that
        made it, and a better detector is a parser change - relabelling in place
        would make the store disagree with its own `parser_config_sha256`. This
        answers whether a re-parse is worth its cost, which is a measured decision
        and not one to take blind (defect D10, slice 2.15).
        """
        moves: Counter[tuple[str, str]] = Counter()
        examples: dict[tuple[str, str], list[str]] = {}
        total = 0
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor(name="language_drift") as cursor:
                cursor.itersize = 200
                cursor.execute(
                    "SELECT versions.version_id, versions.language, versions.canonical_text,"
                    " documents.canonical_url"
                    " FROM kx.document_versions AS versions"
                    " JOIN kx.documents AS documents USING (document_id)"
                    " WHERE versions.is_complete"
                )
                for row in cursor:
                    total += 1
                    stored = str(row["language"])
                    detected = language_of(str(row["canonical_text"]))
                    if detected == stored:
                        continue
                    key = (stored, detected)
                    moves[key] += 1
                    if len(examples.setdefault(key, [])) < 3:
                        examples[key].append(str(row["canonical_url"]))
        return {
            "versionsExamined": total,
            "unchanged": total - sum(moves.values()),
            "changed": sum(moves.values()),
            "moves": [
                {
                    "from": stored,
                    "to": detected,
                    "count": count,
                    "examples": examples[(stored, detected)],
                }
                for (stored, detected), count in moves.most_common(limit)
            ],
        }

    def record_wiki_snapshot(
        self, snapshot: WikiSnapshot, *, recorded_by: str, notes: str | None = None
    ) -> dict[str, Any]:
        """Store one snapshot of the file wiki, or recognise one already stored.

        Content-addressed twice over. The snapshot id comes from the manifest hash,
        so a second snapshot of unchanged files is the same snapshot and says so.
        Blobs are keyed by their own hash, so the 30 MB PDF under `raw/originals`
        is stored once however many snapshots contain it.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT taken_at, file_count FROM kx.wiki_snapshots WHERE snapshot_id = %s",
                    (snapshot.snapshot_id,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    return {
                        "snapshotId": snapshot.snapshot_id,
                        "manifestSha256": snapshot.manifest_sha256,
                        "alreadyStored": True,
                        "takenAt": str(existing["taken_at"]),
                        "fileCount": int(cast(int, existing["file_count"])),
                    }

                stored_blobs = 0
                stored_bytes = 0
                for item in snapshot.files:
                    payload = compress(item.content)
                    cursor.execute(
                        """
                        INSERT INTO kx.wiki_blobs
                            (blob_sha256, compression, raw_bytes, stored_bytes, content)
                        VALUES (%s, 'gzip', %s, %s, %s)
                        ON CONFLICT (blob_sha256) DO NOTHING
                        """,
                        (item.blob_sha256, item.bytes_, len(payload), payload),
                    )
                    if cursor.rowcount:
                        stored_blobs += 1
                        stored_bytes += len(payload)
                cursor.execute(
                    """
                    INSERT INTO kx.wiki_snapshots
                        (snapshot_id, taken_at, manifest_sha256, perimeter,
                         file_count, total_bytes, notes, recorded_by)
                    VALUES (%s, clock_timestamp(), %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.manifest_sha256,
                        snapshot.perimeter,
                        len(snapshot.files),
                        snapshot.total_bytes,
                        notes,
                        recorded_by,
                    ),
                )
                for item in snapshot.files:
                    cursor.execute(
                        "INSERT INTO kx.wiki_snapshot_files"
                        " (snapshot_id, relative_path, blob_sha256, bytes)"
                        " VALUES (%s, %s, %s, %s)",
                        (snapshot.snapshot_id, item.relative_path, item.blob_sha256, item.bytes_),
                    )
        return {
            "snapshotId": snapshot.snapshot_id,
            "manifestSha256": snapshot.manifest_sha256,
            "alreadyStored": False,
            "fileCount": len(snapshot.files),
            "totalBytes": snapshot.total_bytes,
            "newBlobs": stored_blobs,
            "newStoredBytes": stored_bytes,
        }

    def wiki_snapshots(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT snapshot_id, taken_at, perimeter, file_count, total_bytes,"
                " manifest_sha256, recorded_by, notes"
                " FROM kx.wiki_snapshots ORDER BY taken_at DESC LIMIT %s",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ---------------------------------------------------------------------
    # The authored wiki as concepts (slice 2.5, P24)
    # ---------------------------------------------------------------------

    def snapshot_pages(self, snapshot_id: str, *, perimeter: str) -> list[ParsedPage]:
        """Parse the authored markdown of one stored snapshot.

        Read out of the store rather than off a filesystem: the snapshot is
        content-addressed and immutable, so an import is reproducible from KX
        alone and a concept version can name exactly which bytes it came from.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT files.relative_path, blobs.content"
                    " FROM kx.wiki_snapshot_files AS files"
                    " JOIN kx.wiki_blobs AS blobs USING (blob_sha256)"
                    " WHERE files.snapshot_id = %s ORDER BY files.relative_path",
                    (snapshot_id,),
                )
                rows = cursor.fetchall()
        if not rows:
            raise ValueError(f"snapshot {snapshot_id} holds no files")
        pages: list[ParsedPage] = []
        for row in rows:
            relative = str(row["relative_path"])
            if not relative.endswith(".md") or not is_authored(relative, perimeter=perimeter):
                continue
            body = gzip.decompress(cast(bytes, row["content"])).decode("utf-8", errors="replace")
            pages.append(parse_page(relative, body, perimeter=perimeter))
        return pages

    def import_wiki_concepts(
        self, *, snapshot_id: str, perimeter: str, imported_by: str
    ) -> dict[str, Any]:
        """Import one snapshot's authored pages as concepts and their statements."""
        pages = self.snapshot_pages(snapshot_id, perimeter=perimeter)
        imported: dict[str, Any] = {
            "snapshotId": snapshot_id,
            "pages": len(pages),
            "concepts": 0,
            "versions": 0,
            "sections": 0,
            "mappedSections": 0,
            "statements": 0,
            "alreadyImported": 0,
        }
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for page in pages:
                    cursor.execute(
                        """
                        INSERT INTO kx.concepts (relative_path, perimeter, layer, created_by)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (perimeter, relative_path)
                            DO UPDATE SET relative_path = EXCLUDED.relative_path
                        RETURNING concept_id
                        """,
                        (page.relative_path, perimeter, page.layer, imported_by),
                    )
                    concept_id = str(one_row(cursor)["concept_id"])
                    imported["concepts"] = int(imported["concepts"]) + 1
                    version_id = page.concept_version_id(concept_id, snapshot_id)
                    cursor.execute(
                        """
                        INSERT INTO kx.concept_versions
                            (concept_version_id, concept_id, snapshot_id, title, body,
                             body_sha256, word_count, language, imported_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (concept_version_id) DO NOTHING
                        """,
                        (
                            version_id,
                            concept_id,
                            snapshot_id,
                            page.title,
                            page.body,
                            page.body_sha256,
                            page.word_count,
                            page.language,
                            imported_by,
                        ),
                    )
                    if not cursor.rowcount:
                        imported["alreadyImported"] = int(imported["alreadyImported"]) + 1
                        continue
                    imported["versions"] = int(imported["versions"]) + 1
                    section_ids: dict[int, str] = {}
                    for section in page.sections:
                        cursor.execute(
                            """
                            INSERT INTO kx.concept_sections
                                (concept_version_id, ordinal, heading, heading_level,
                                 convention, char_start, char_end)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            RETURNING section_id
                            """,
                            (
                                version_id,
                                section.ordinal,
                                section.heading,
                                section.heading_level,
                                section.convention,
                                section.char_start,
                                section.char_end,
                            ),
                        )
                        section_ids[section.ordinal] = str(one_row(cursor)["section_id"])
                        imported["sections"] = int(imported["sections"]) + 1
                        if section.convention:
                            imported["mappedSections"] = int(imported["mappedSections"]) + 1
                    for statement in page.statements:
                        cursor.execute(
                            """
                            INSERT INTO kx.concept_claims
                                (concept_version_id, section_id, ordinal, char_start,
                                 char_end, statement, statement_sha256, claim_nature,
                                 segmentation)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'list_item')
                            """,
                            (
                                version_id,
                                section_ids[statement.section_ordinal],
                                statement.ordinal,
                                statement.char_start,
                                statement.char_end,
                                statement.statement,
                                statement.statement_sha256,
                                statement.claim_nature,
                            ),
                        )
                        imported["statements"] = int(imported["statements"]) + 1
        return imported

    def bind_concept_evidence(
        self,
        *,
        snapshot_id: str,
        scope: str,
        per_statement: int = 5,
        floor: float = DEFAULT_RELEVANCE_FLOOR,
        created_by: str,
    ) -> dict[str, Any]:
        """Propose evidence for every statement of one snapshot. Confirms nothing."""
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {sorted(SCOPES)}")
        sql = EVIDENCE_SEARCH_SQL.format(scope=SCOPES[scope])
        outcome: dict[str, Any] = {
            "snapshotId": snapshot_id,
            "membershipClass": scope,
            "statements": 0,
            "statementsWithProposal": 0,
            "proposals": 0,
            "floor": floor,
        }
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT claims.concept_claim_id, claims.statement"
                    " FROM kx.concept_claims AS claims"
                    " JOIN kx.concept_versions AS versions USING (concept_version_id)"
                    " WHERE versions.snapshot_id = %s",
                    (snapshot_id,),
                )
                statements = [
                    (str(row["concept_claim_id"]), str(row["statement"]))
                    for row in cursor.fetchall()
                ]
            outcome["statements"] = len(statements)
            for concept_claim_id, statement in statements:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute(
                        sql,
                        {
                            "statement": statement,
                            "k": RRF_K,
                            "limit": per_statement,
                        },
                    )
                    hits = [
                        (str(row["claim_id"]), float(row["relevance"]))
                        for row in cursor.fetchall()
                        if float(row["relevance"]) >= floor
                    ]
                    if hits:
                        outcome["statementsWithProposal"] = (
                            int(outcome["statementsWithProposal"]) + 1
                        )
                    for claim_id, relevance in hits:
                        cursor.execute(
                            """
                            INSERT INTO kx.concept_evidence
                                (concept_claim_id, claim_id, membership_class,
                                 binding_method, relevance, created_by)
                            VALUES (%s, %s, %s, 'search_proposed', %s, %s)
                            ON CONFLICT (concept_claim_id, claim_id) DO NOTHING
                            """,
                            (concept_claim_id, claim_id, scope, relevance, created_by),
                        )
                        outcome["proposals"] = int(outcome["proposals"]) + cursor.rowcount
        return outcome

    def statements_without_evidence(self, *, snapshot_id: str) -> dict[str, Any]:
        """The report ADR-0008 §2.3 asks for.

        A statement with no confirmed binding is not thereby false. It is
        unsupported, it is counted, and it is not published as evidence-backed.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT support.claim_nature,
                           count(*) AS statements,
                           count(*) FILTER (WHERE support.confirmed_bindings > 0) AS supported,
                           count(*) FILTER (
                               WHERE support.confirmed_bindings = 0
                                 AND support.proposed_bindings > 0
                           ) AS proposed_only,
                           count(*) FILTER (WHERE support.proposed_bindings = 0) AS untouched
                    FROM kx.concept_claim_support AS support
                    JOIN kx.concept_versions AS versions USING (concept_version_id)
                    WHERE versions.snapshot_id = %s
                    GROUP BY support.claim_nature
                    ORDER BY statements DESC
                    """,
                    (snapshot_id,),
                )
                by_nature = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT concepts.relative_path, count(*) AS statements
                    FROM kx.concept_claim_support AS support
                    JOIN kx.concept_versions AS versions USING (concept_version_id)
                    JOIN kx.concepts AS concepts USING (concept_id)
                    WHERE versions.snapshot_id = %s AND support.proposed_bindings = 0
                    GROUP BY concepts.relative_path
                    ORDER BY statements DESC
                    LIMIT 15
                    """,
                    (snapshot_id,),
                )
                worst = [dict(row) for row in cursor.fetchall()]
        total = sum(int(row["statements"]) for row in by_nature)
        supported = sum(int(row["supported"]) for row in by_nature)
        return {
            "snapshotId": snapshot_id,
            "statements": total,
            "withConfirmedEvidence": supported,
            "withProposalsOnly": sum(int(row["proposed_only"]) for row in by_nature),
            "withNothing": sum(int(row["untouched"]) for row in by_nature),
            # byNature, never averaged: an open question and a normative statement
            # are not the same kind of thing to be unsupported.
            "byNature": by_nature,
            "pagesWithNothing": worst,
        }

    # ---------------------------------------------------------------------
    # Acquisition as a subsystem (slice 2.3)
    # ---------------------------------------------------------------------

    def host_profiles(self) -> dict[str, HostProfile]:
        """Every written profile. A host without one gets the default."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT host, rung_order, min_interval_seconds, max_in_flight,"
                " robots_policy, request_headers, rationale, decided_by"
                " FROM kx.host_profiles"
            )
            profiles: dict[str, HostProfile] = {}
            for row in cursor.fetchall():
                rungs = row["rung_order"]
                profiles[str(row["host"])] = HostProfile(
                    host=str(row["host"]),
                    rungs=tuple(cast(list[str], rungs)) if rungs is not None else ("network",),
                    min_interval_seconds=(
                        float(cast(float, row["min_interval_seconds"]))
                        if row["min_interval_seconds"] is not None
                        else None
                    ),
                    max_in_flight=(
                        int(cast(int, row["max_in_flight"]))
                        if row["max_in_flight"] is not None
                        else None
                    ),
                    robots_policy=str(row["robots_policy"]),
                    request_headers=cast(dict[str, str], row["request_headers"]),
                    rationale=str(row["rationale"]),
                    decided_by=str(row["decided_by"]),
                )
        return profiles

    def write_host_profile(self, profile: HostProfile) -> dict[str, Any]:
        """Record a decision about how one host is treated."""
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.host_profiles
                        (host, rung_order, min_interval_seconds, max_in_flight,
                         robots_policy, request_headers, rationale, decided_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (host) DO UPDATE SET
                        rung_order = EXCLUDED.rung_order,
                        min_interval_seconds = EXCLUDED.min_interval_seconds,
                        max_in_flight = EXCLUDED.max_in_flight,
                        robots_policy = EXCLUDED.robots_policy,
                        request_headers = EXCLUDED.request_headers,
                        rationale = EXCLUDED.rationale,
                        decided_by = EXCLUDED.decided_by,
                        decided_at = clock_timestamp()
                    """,
                    (
                        profile.host,
                        list(profile.rungs),
                        profile.min_interval_seconds,
                        profile.max_in_flight,
                        profile.robots_policy,
                        Jsonb(dict(profile.request_headers)),
                        profile.rationale,
                        profile.decided_by,
                    ),
                )
        return profile.as_json()

    def plan_acquisition(self, *, limit: int = 500) -> dict[str, Any]:
        """Walk the gap queue and record where each document is on the ladder.

        Decides nothing about the network: it reads what already happened, applies
        the host's profile, and writes the next rung or a terminal reason with an
        owner. Running it twice changes nothing that has not changed underneath.
        """
        profiles = self.host_profiles()
        planned: dict[str, Any] = {
            "considered": 0,
            "escalated": 0,
            "terminal": 0,
            "byReason": {},
        }
        reasons: dict[str, int] = {}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT document_id, canonical_url, current_rung, rungs_tried,"
                    " last_error_code, terminal_reason"
                    " FROM kx.acquisition_gap_queue"
                    " WHERE terminal_reason IS NULL"
                    " ORDER BY updated_at LIMIT %s",
                    (limit,),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                planned["considered"] = int(planned["considered"]) + 1
                tried = list(cast(list[str], row["rungs_tried"] or []))
                if str(row["current_rung"]) not in tried:
                    tried.append(str(row["current_rung"]))
                step = next_step(
                    profile=profile_for(str(row["canonical_url"]), profiles),
                    tried=tried,
                    error_code=(str(row["last_error_code"]) if row["last_error_code"] else None),
                )
                with connection.transaction(), connection.cursor() as cursor:
                    if step.is_terminal:
                        cursor.execute(
                            "UPDATE kx.fetch_queue SET rungs_tried = %s,"
                            " terminal_reason = %s, next_action_owner = %s,"
                            " updated_at = clock_timestamp() WHERE document_id = %s",
                            (
                                tried,
                                step.terminal_reason,
                                step.next_action_owner,
                                row["document_id"],
                            ),
                        )
                        planned["terminal"] = int(planned["terminal"]) + 1
                        key = str(step.terminal_reason)
                        reasons[key] = reasons.get(key, 0) + 1
                    else:
                        cursor.execute(
                            "UPDATE kx.fetch_queue SET current_rung = %s, rungs_tried = %s,"
                            " status = 'retry', next_attempt_at = clock_timestamp(),"
                            " next_action_owner = 'machine', updated_at = clock_timestamp()"
                            " WHERE document_id = %s",
                            (step.rung, tried, row["document_id"]),
                        )
                        planned["escalated"] = int(planned["escalated"]) + 1
        planned["byReason"] = reasons
        return planned

    def acquisition_gaps(self) -> dict[str, Any]:
        """What is missing, why, and whose move it is."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT coalesce(terminal_reason, 'undecided') AS reason,"
                " coalesce(next_action_owner, 'unassigned') AS owner,"
                " coalesce(last_error_code, 'none') AS error_code,"
                " count(*) AS total"
                " FROM kx.acquisition_gap_queue GROUP BY 1, 2, 3 ORDER BY total DESC"
            )
            rows = [dict(row) for row in cursor.fetchall()]
        return {
            "documentsWithoutText": sum(int(row["total"]) for row in rows),
            "byReason": rows[:40],
        }

    # ---------------------------------------------------------------------
    # Candidate ideas (slice 2.9, P13 and ADR-0007 §4)
    # ---------------------------------------------------------------------

    def claims_for_ideas(self, *, scope: str, limit: int = 4000) -> list[ClaimRecord]:
        """Accepted-or-proposed claims in scope, with the document behind each."""
        source = SCOPES[scope]
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    WITH scope AS ({source})
                    SELECT claims.claim_id,
                           versions.document_id,
                           claims.predicate,
                           claims.object_text,
                           evidence.quote_text
                    FROM kx.claims AS claims
                    JOIN kx.claim_evidence AS evidence USING (claim_id)
                    JOIN kx.document_versions AS versions
                      ON versions.version_id = claims.version_id
                    JOIN scope ON scope.document_id = versions.document_id
                    WHERE claims.state <> 'rejected' AND evidence.match_status = 'exact'
                    ORDER BY claims.claim_id
                    LIMIT %s
                    """,  # noqa: S608 - a constant from SCOPES
                    (limit,),
                )
                return [
                    ClaimRecord(
                        claim_id=str(row["claim_id"]),
                        document_id=str(row["document_id"]),
                        predicate=str(row["predicate"]),
                        object_text=str(row["object_text"]),
                        quote_text=str(row["quote_text"]),
                    )
                    for row in cursor.fetchall()
                ]

    def independence_verdict(self, document_ids: Sequence[str]) -> IndependenceVerdict:
        """The counting rules, plus the version of the data they were applied to."""
        report = self.independence_report(document_ids)
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT coalesce(max(decision_id), 0) AS high_water FROM kx.source_family_decisions"
            )
            high_water = int(cast(int, one_row(cursor)["high_water"]))
            cursor.execute(
                "SELECT count(*) AS total FROM kx.content_duplicate_clusters"
                " WHERE confirmed_at IS NOT NULL"
            )
            clusters = int(cast(int, one_row(cursor)["total"]))
        return IndependenceVerdict(
            independent_sources=int(report["independentSources"]),
            unknown_documents=int(report["unknownDocuments"]),
            collapsed_by_family=int(report["collapsedByFamily"]),
            collapsed_by_cluster=int(report["collapsedByCluster"]),
            family_decision_high_water=high_water,
            confirmed_cluster_count=clusters,
        )

    def propose_candidate_groups(
        self, *, scope: str, threshold: float, limit: int = 4000
    ) -> list[tuple[CandidateGroup, IndependenceVerdict]]:
        """Group claims and judge each group. Writes nothing."""
        claims = self.claims_for_ideas(scope=scope, limit=limit)
        groups = group_claims(claims, threshold=threshold)
        return [(group, self.independence_verdict(group.document_ids)) for group in groups]

    def record_idea(
        self,
        group: CandidateGroup,
        verdict: IndependenceVerdict,
        *,
        title: str,
        statement: str,
        created_by: str,
        model: str | None = None,
        prompt_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Record one candidate idea with the verdict it was judged on.

        An idea that failed the gate is recorded too, and stays `proposed`: a
        CHECK stops it ever being shown. "Nothing was proposed this week" and
        "eleven were proposed and none had two independent sources" are different
        facts about the corpus, and only the second one is true today.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.ideas
                        (title, statement, created_by, model, prompt_sha256,
                         independent_sources, unknown_documents, collapsed_by_family,
                         collapsed_by_cluster, family_decision_high_water,
                         confirmed_cluster_count, admitted)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING idea_id
                    """,
                    (
                        title,
                        statement,
                        created_by,
                        model,
                        prompt_sha256,
                        verdict.independent_sources,
                        verdict.unknown_documents,
                        verdict.collapsed_by_family,
                        verdict.collapsed_by_cluster,
                        verdict.family_decision_high_water,
                        verdict.confirmed_cluster_count,
                        verdict.admitted,
                    ),
                )
                idea_id = str(one_row(cursor)["idea_id"])
                for claim in group.claims:
                    cursor.execute(
                        "INSERT INTO kx.idea_evidence (idea_id, claim_id, stance)"
                        " VALUES (%s, %s, 'support')",
                        (idea_id, claim.claim_id),
                    )
        return {"ideaId": idea_id, "admitted": verdict.admitted, **verdict.as_json()}

    def idea_report(self) -> dict[str, Any]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, admitted, count(*) AS total,"
                " min(independent_sources) AS min_sources,"
                " max(independent_sources) AS max_sources"
                " FROM kx.ideas GROUP BY state, admitted ORDER BY total DESC"
            )
            rows = [dict(row) for row in cursor.fetchall()]
        return {
            "ideas": sum(int(row["total"]) for row in rows),
            "admitted": sum(int(row["total"]) for row in rows if row["admitted"]),
            "byState": rows,
        }

    # ---------------------------------------------------------------------
    # Publication of the structural layer (slice 2.8, P19)
    # ---------------------------------------------------------------------

    def publishable_quotes(self, *, scope: str, limit: int = 200) -> list[dict[str, Any]]:
        """Exact quotations in scope, with everything the five conditions need.

        One query, because asking five questions per claim across eight thousand
        claims is the difference between a report and an afternoon.
        """
        source = _SCOPE_SOURCES[scope]
        query = f"""
            SELECT evidence.claim_id,
                   evidence.version_id,
                   evidence.char_start,
                   evidence.char_end,
                   evidence.quote_text,
                   versions.canonical_text,
                   versions.language,
                   documents.canonical_url,
                   documents.document_id,
                   blocked.block_reason,
                   caveat.caveat_detail,
                   translations.translation_id,
                   translations.translated_text,
                   translations.target_language,
                   translations.invariant_report
            FROM kx.claim_evidence AS evidence
            JOIN kx.document_versions AS versions USING (version_id)
            JOIN kx.documents AS documents
              ON documents.document_id = versions.document_id
            JOIN ({source}) AS scoped ON scoped.document_id = versions.document_id
            LEFT JOIN kx.version_publication_block AS blocked
                   ON blocked.version_id = versions.version_id
            LEFT JOIN kx.version_publication_caveat AS caveat
                   ON caveat.version_id = versions.version_id
            LEFT JOIN kx.quote_translations AS translations
                   ON translations.claim_id = evidence.claim_id
                  AND translations.state <> 'rejected'
            WHERE evidence.match_status = 'exact'
              AND NOT EXISTS (
                  SELECT 1 FROM kx.published_quotes AS published
                  WHERE published.claim_id = evidence.claim_id
                    AND published.translation_id IS NOT DISTINCT FROM translations.translation_id
              )
            ORDER BY evidence.claim_id
            LIMIT %s
            """  # noqa: S608 - a constant from _SCOPE_SOURCES
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(query, (limit,))
                return [dict(row) for row in cursor.fetchall()]

    def publish_quotes(
        self,
        *,
        scope: str,
        limit: int = 200,
        target_language: str = "ru",
        attribution_suffix: str = "",
    ) -> dict[str, Any]:
        """Publish what clears the five conditions; quarantine the rest with a remedy.

        No approval gate, manual or batch: that is what P19 decided. What replaces
        it is that every condition is checked here and every failure is written
        down with what would clear it.

        A quotation in a language the reader does not have, and with no verified
        translation yet, is **skipped rather than quarantined**. Quarantine is for
        an item that failed a condition; work that has not been done yet is not a
        failure, and mixing the two makes the queue unreadable. It also has to be
        skipped rather than published bare: a published row is immutable, so
        publishing the original now would leave nowhere for the translation to go.
        """
        candidates = self.publishable_quotes(scope=scope, limit=limit)
        published = 0
        awaiting_translation = 0
        quarantined: dict[str, int] = {}
        with self.connect() as connection:
            self.require_schema(connection)
            for row in candidates:
                if str(row["language"]) != target_language and row["translation_id"] is None:
                    awaiting_translation += 1
                    continue
                report = None
                if row["invariant_report"]:
                    payload = cast(dict[str, Any], row["invariant_report"])
                    report = InvariantReport(
                        numbers_match=bool(payload.get("numbersMatch")),
                        original_numbers=tuple(payload.get("originalNumbers") or ()),
                        translated_numbers=tuple(payload.get("translatedNumbers") or ()),
                        symbols_match=bool(payload.get("symbolsMatch")),
                        original_symbols=payload.get("originalSymbols") or {},
                        translated_symbols=payload.get("translatedSymbols") or {},
                    )
                verdict = self.independence_report([str(row["document_id"])])
                decision = decide(
                    canonical_text=str(row["canonical_text"]),
                    char_start=int(cast(int, row["char_start"])),
                    char_end=int(cast(int, row["char_end"])),
                    quote_text=str(row["quote_text"]),
                    block_reason=(str(row["block_reason"]) if row["block_reason"] else None),
                    caveat=str(row["caveat_detail"]) if row["caveat_detail"] else None,
                    invariants=report,
                    independent_sources=int(verdict["independentSources"]),
                    # A quotation is evidence of what one source said. Independence
                    # is a property of a claim resting on several, and applying it
                    # to a single quotation would withhold every source that is the
                    # only one to have said something - which is most of them.
                    independence_required=False,
                )
                with connection.transaction(), connection.cursor() as cursor:
                    if decision.publishable:
                        cursor.execute(
                            """
                            INSERT INTO kx.published_quotes
                                (claim_id, version_id, char_start, char_end, original_text,
                                 translation_id, quote_chars, attribution, source_url,
                                 caveat, published_automatically, independence_sources)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                row["claim_id"],
                                row["version_id"],
                                row["char_start"],
                                row["char_end"],
                                row["quote_text"],
                                row["translation_id"],
                                int(cast(int, row["char_end"])) - int(cast(int, row["char_start"])),
                                f"{row['canonical_url']}{attribution_suffix}",
                                row["canonical_url"],
                                decision.caveat,
                                int(verdict["independentSources"]),
                            ),
                        )
                        published += cursor.rowcount
                        # The thing `what_would_clear_it` asked for happened. A
                        # queue that keeps entries whose condition is gone is a
                        # queue that reports work nobody has to do: the first
                        # production run left 298 provenance failures standing
                        # after the provenance was recorded.
                        continue
                    for item in decision.quarantine:
                        # One open entry per claim and condition. Without this a
                        # queue of 69 stuck claims reads as 180 after three runs,
                        # and the number people act on is the wrong one.
                        cursor.execute(
                            """
                            INSERT INTO kx.publication_quarantine
                                (claim_id, translation_id, failed_condition, detail,
                                 what_would_clear_it)
                            SELECT %s, %s, %s, %s, %s
                            WHERE NOT EXISTS (
                                SELECT 1 FROM kx.publication_quarantine AS existing
                                WHERE existing.claim_id = %s
                                  AND existing.failed_condition = %s
                                  AND existing.resolved_at IS NULL
                            )
                            """,
                            (
                                row["claim_id"],
                                row["translation_id"],
                                item.failed_condition,
                                item.detail,
                                item.what_would_clear_it,
                                row["claim_id"],
                                item.failed_condition,
                            ),
                        )
                        if cursor.rowcount:
                            quarantined[item.failed_condition] = (
                                quarantined.get(item.failed_condition, 0) + 1
                            )
        resolved = self.resolve_quarantine_for_published()
        return {
            "scope": scope,
            "targetLanguage": target_language,
            "considered": len(candidates),
            "published": published,
            "awaitingTranslation": awaiting_translation,
            "quarantineResolved": resolved,
            "quarantined": sum(quarantined.values()),
            "byFailedCondition": quarantined,
        }

    def resolve_quarantine_for_published(self) -> int:
        """Close every open entry whose claim has since been published.

        An open entry for a published claim is not open: the thing
        `what_would_clear_it` asked for happened. Sweeping all of them rather than
        only this batch's is deliberate - the first production run quarantined 298
        claims for missing provenance, the provenance was recorded, the claims
        published in a later batch, and the 298 rows stayed standing because
        nothing revisited them.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE kx.publication_quarantine AS quarantine
                    SET resolved_at = clock_timestamp(), resolved_by = 'published'
                    WHERE quarantine.resolved_at IS NULL
                      AND EXISTS (
                          SELECT 1 FROM kx.published_quotes AS published
                          WHERE published.claim_id = quarantine.claim_id
                      )
                    """
                )
                return cursor.rowcount

    def record_translation(
        self,
        *,
        claim_id: str,
        version_id: str,
        char_start: int,
        char_end: int,
        original_text: str,
        source_language: str,
        target_language: str,
        translated_text: str,
        translator: str,
        is_machine: bool,
        prompt_sha256: str | None,
        report: InvariantReport,
        created_by: str,
    ) -> dict[str, Any]:
        """Store one translation with the invariant check that judged it."""
        state = "rejected" if report.blocking else "verified"
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.quote_translations
                        (claim_id, version_id, char_start, char_end, original_text,
                         source_language, target_language, translated_text, translator,
                         is_machine, prompt_sha256, state, invariant_report, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING translation_id
                    """,
                    (
                        claim_id,
                        version_id,
                        char_start,
                        char_end,
                        original_text,
                        source_language,
                        target_language,
                        translated_text,
                        translator,
                        is_machine,
                        prompt_sha256,
                        state,
                        Jsonb(report.as_json()),
                        created_by,
                    ),
                )
                row = cursor.fetchone()
                translation_id = str(row["translation_id"]) if row else None
                # P36: an unregistered spelling never blocks. The name is shown in
                # its original form and the proposal waits with no deadline.
                for name in report.unresolved_names:
                    cursor.execute(
                        """
                        INSERT INTO kx.entity_alias_proposals
                            (original_form, proposed_form, language, seen_in_translation)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (original_form, proposed_form, language)
                            DO UPDATE SET occurrences = kx.entity_alias_proposals.occurrences + 1
                        """,
                        (name, name, target_language, translation_id),
                    )
        return {
            "translationId": translation_id,
            "state": state,
            "aliasProposals": len(report.unresolved_names),
            **report.as_json(),
        }

    def publication_report(self) -> dict[str, Any]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) AS total,"
                " count(*) FILTER (WHERE published_automatically) AS automatic,"
                " count(*) FILTER (WHERE caveat IS NOT NULL) AS with_caveat"
                " FROM kx.published_quotes"
            )
            published = dict(one_row(cursor))
            cursor.execute(
                "SELECT failed_condition, count(*) AS total FROM kx.publication_quarantine"
                " WHERE resolved_at IS NULL GROUP BY 1 ORDER BY total DESC"
            )
            quarantine = {
                str(row["failed_condition"]): int(row["total"]) for row in cursor.fetchall()
            }
            cursor.execute("SELECT state, count(*) AS total FROM kx.quote_translations GROUP BY 1")
            translations = {str(row["state"]): int(row["total"]) for row in cursor.fetchall()}
            cursor.execute(
                "SELECT count(*) AS total FROM kx.entity_alias_proposals WHERE decided_at IS NULL"
            )
            aliases = int(cast(int, one_row(cursor)["total"]))
        return {
            "published": {key: int(value) for key, value in published.items()},
            "quarantineByCondition": quarantine,
            "translations": translations,
            "openAliasProposals": aliases,
        }

    #: Which acquisition method a recorded fetch attempt corresponds to. Not a
    #: guess: `fetch_attempts.source_kind` is the record the worker wrote at the
    #: moment it obtained the bytes, and this is the same fact under the
    #: vocabulary `version_provenance` uses.
    FETCH_KIND_TO_ACCESS_METHOD: ClassVar[dict[str, str]] = {
        "network": "http_default",
        "network_browser_headers": "browser_headers",
        "network_robots_override": "robots_override",
        "browser_render": "browser_render",
        "web_archive": "web_archive",
        "legacy_snapshot": "http_default",
        "legacy_truncated": "http_default",
    }

    def backfill_provenance_from_fetches(self, *, limit: int = 20000) -> dict[str, Any]:
        """Give network-acquired versions the provenance their fetch already records.

        Migration 003 made provenance a precondition of publication, and the
        backfill that came with it covered the 25 documents an operator had handed
        over. Everything the worker fetched itself - 6 464 complete versions - had
        none, so `version_publication_block` withheld all of it: the first
        automatic publication run published 46 quotations and quarantined 298 for
        `provenance_invalid`.

        Nothing is invented here. `fetch_attempts` is the row the worker wrote at
        the moment it obtained the bytes, matched to the version by the raw hash,
        and it already says which rung produced them, when, and under which
        release. This restates that fact in the vocabulary publication reads.
        """
        recorded: dict[str, int] = {}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT versions.version_id,
                           attempts.source_kind,
                           attempts.finished_at,
                           attempts.worker_release,
                           documents.canonical_url
                    FROM kx.document_versions AS versions
                    JOIN kx.documents AS documents USING (document_id)
                    LEFT JOIN kx.version_provenance_current AS current
                           ON current.version_id = versions.version_id
                    JOIN LATERAL (
                        SELECT source_kind, finished_at, worker_release
                        FROM kx.fetch_attempts AS attempt
                        WHERE attempt.document_id = versions.document_id
                          AND attempt.raw_sha256 = versions.raw_sha256
                          AND attempt.outcome = 'succeeded'
                        ORDER BY attempt.finished_at
                        LIMIT 1
                    ) AS attempts ON true
                    WHERE current.version_id IS NULL
                    ORDER BY versions.version_id
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                method = self.FETCH_KIND_TO_ACCESS_METHOD.get(str(row["source_kind"]))
                if method is None:
                    recorded["unmapped:" + str(row["source_kind"])] = (
                        recorded.get("unmapped:" + str(row["source_kind"]), 0) + 1
                    )
                    continue
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO kx.version_provenance
                            (version_id, source_access_method, browser_used, provided_by,
                             provided_at, original_url, notes, recorded_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            row["version_id"],
                            method,
                            method == "browser_render",
                            row["worker_release"],
                            row["finished_at"],
                            row["canonical_url"],
                            "restated from the fetch attempt that produced these bytes",
                            "radar-kx-backfill-provenance-from-fetches",
                        ),
                    )
                recorded[method] = recorded.get(method, 0) + 1
        return {"considered": len(rows), "recorded": recorded}

    # ---------------------------------------------------------------------
    # The graph (slice 2.11)
    # ---------------------------------------------------------------------

    def build_graph(self, *, wiki_snapshot_id: str | None = None) -> Graph:
        """Read the store into a graph. Writes nothing."""
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT versions.concept_id, versions.title, versions.language,"
                    " concepts.relative_path, concepts.layer"
                    " FROM kx.concept_versions AS versions"
                    " JOIN kx.concepts AS concepts USING (concept_id)"
                    " WHERE %(snapshot)s::text IS NULL"
                    "    OR versions.snapshot_id = %(snapshot)s",
                    {"snapshot": wiki_snapshot_id},
                )
                concepts = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT claims.concept_claim_id, claims.statement, claims.claim_nature,"
                    " claims.segmentation, versions.concept_id"
                    " FROM kx.concept_claims AS claims"
                    " JOIN kx.concept_versions AS versions USING (concept_version_id)"
                    " WHERE %(snapshot)s::text IS NULL"
                    "    OR versions.snapshot_id = %(snapshot)s",
                    {"snapshot": wiki_snapshot_id},
                )
                concept_claims = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT concept_claim_id, claim_id, membership_class, confirmed_at"
                    " FROM kx.concept_evidence"
                )
                concept_evidence = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT idea_id, title, state, admitted, independent_sources FROM kx.ideas"
                )
                ideas = [dict(row) for row in cursor.fetchall()]
                cursor.execute("SELECT idea_id, claim_id, stance FROM kx.idea_evidence")
                idea_evidence = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT claims.claim_id, claims.state, evidence.version_id,"
                    " evidence.char_start, evidence.char_end, evidence.quote_text,"
                    " versions.document_id, versions.language, documents.canonical_url"
                    " FROM kx.claims AS claims"
                    " JOIN kx.claim_evidence AS evidence USING (claim_id)"
                    " JOIN kx.document_versions AS versions"
                    "   ON versions.version_id = evidence.version_id"
                    " JOIN kx.documents AS documents USING (document_id)"
                    " WHERE evidence.match_status = 'exact'"
                )
                claims = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT current.document_id, current.family_id, current.family_key,"
                    " current.family_kind FROM kx.document_source_family_current AS current"
                    " WHERE current.decision_action <> 'retired'"
                )
                families = [dict(row) for row in cursor.fetchall()]
        return build_graph(
            concepts=concepts,
            concept_claims=concept_claims,
            concept_evidence=concept_evidence,
            ideas=ideas,
            idea_evidence=idea_evidence,
            claims=claims,
            families=families,
        )

    def record_graph_snapshot(
        self, graph: Graph, *, built_by: str, wiki_snapshot_id: str | None = None
    ) -> dict[str, Any]:
        """Store one graph, or recognise one already stored.

        Content-addressed like the wiki snapshot: the same store projects to the
        same identifier, so rebuilding an unchanged graph records nothing twice
        and a release can point at a snapshot that means one thing forever.
        """
        loose = dangling(graph)
        if loose:
            raise RuntimeError(f"{len(loose)} edges point outside the graph")
        snapshot_id = graph.snapshot_id()
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT built_at FROM kx.graph_snapshots WHERE graph_snapshot_id = %s",
                    (snapshot_id,),
                )
                if cursor.fetchone() is not None:
                    return {**graph.as_json(), "alreadyStored": True}
                cursor.execute(
                    "SELECT coalesce(max(decision_id), 0) AS high_water"
                    " FROM kx.source_family_decisions"
                )
                high_water = int(cast(int, one_row(cursor)["high_water"]))
                cursor.execute(
                    """
                    INSERT INTO kx.graph_snapshots
                        (graph_snapshot_id, wiki_snapshot_id, family_decision_high_water,
                         node_count, edge_count, manifest_sha256, built_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        snapshot_id,
                        wiki_snapshot_id,
                        high_water,
                        len(graph.nodes),
                        len(graph.edges),
                        graph.manifest_sha256,
                        built_by,
                    ),
                )
                for node in graph.nodes:
                    cursor.execute(
                        "INSERT INTO kx.graph_nodes"
                        " (graph_snapshot_id, node_id, node_kind, label, natural_key, attributes)"
                        " VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            snapshot_id,
                            node.node_id,
                            node.node_kind,
                            node.label,
                            node.natural_key,
                            Jsonb(node.attributes),
                        ),
                    )
                for edge in graph.edges:
                    cursor.execute(
                        "INSERT INTO kx.graph_edges"
                        " (graph_snapshot_id, from_node_id, to_node_id, relation, attributes)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (
                            snapshot_id,
                            edge.from_node_id,
                            edge.to_node_id,
                            edge.relation,
                            Jsonb(edge.attributes),
                        ),
                    )
        return {
            **graph.as_json(),
            "alreadyStored": False,
            # Not an error: a wiki statement nobody has bound yet is exactly what
            # the "statements without evidence" report counts. Said out loud so
            # the graph does not look complete.
            "unsupported": unsupported(graph),
        }

    # ---------------------------------------------------------------------
    # The knowledge release (slice 3.1, ADR-0006 §1)
    # ---------------------------------------------------------------------

    def _slice_rows(
        self, connection: Connection[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Read what a release may contain: only what somebody already confirmed."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT published.published_quote_id AS quote_id,
                       published.original_text,
                       translations.translated_text,
                       translations.is_machine AS translation_is_machine,
                       published.attribution,
                       published.source_url,
                       published.caveat,
                       published.published_at,
                       published.claim_id
                FROM kx.published_quotes AS published
                LEFT JOIN kx.quote_translations AS translations
                       ON translations.translation_id = published.translation_id
                      AND translations.state = 'verified'
                """
            )
            quotes = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT DISTINCT ON (versions.concept_id)
                       versions.concept_id, versions.title, versions.language,
                       versions.body, versions.body_sha256, concepts.relative_path
                FROM kx.concept_versions AS versions
                JOIN kx.concepts AS concepts USING (concept_id)
                ORDER BY versions.concept_id, versions.imported_at DESC
                """
            )
            concepts = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT claims.concept_claim_id AS statement_id,
                       versions.concept_id,
                       claims.statement,
                       claims.claim_nature,
                       count(evidence.claim_id) FILTER (
                           WHERE evidence.confirmed_at IS NOT NULL
                       )::int AS confirmed_evidence
                FROM kx.concept_claims AS claims
                JOIN kx.concept_versions AS versions USING (concept_version_id)
                LEFT JOIN kx.concept_evidence AS evidence USING (concept_claim_id)
                GROUP BY 1, 2, 3, 4
                """
            )
            statements = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT ideas.idea_id, ideas.title, ideas.statement, ideas.independent_sources
                FROM kx.ideas AS ideas
                WHERE ideas.admitted IS TRUE
                """
            )
            ideas = [dict(row) for row in cursor.fetchall()]
            # A statement's evidence is a confirmed binding to a claim that has a
            # published quotation. A binding to a claim nobody published points at
            # nothing a reader can open.
            cursor.execute(
                """
                SELECT evidence.concept_claim_id AS statement_id,
                       published.published_quote_id AS quote_id,
                       evidence.membership_class
                FROM kx.concept_evidence AS evidence
                JOIN kx.published_quotes AS published USING (claim_id)
                WHERE evidence.confirmed_at IS NOT NULL
                """
            )
            statement_evidence = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT snapshot_id FROM kx.wiki_snapshots ORDER BY taken_at DESC LIMIT 1"
            )
            wiki = cursor.fetchone()
            cursor.execute(
                "SELECT graph_snapshot_id FROM kx.graph_snapshots ORDER BY built_at DESC LIMIT 1"
            )
            graph = cursor.fetchone()
            cursor.execute(
                "SELECT coalesce(max(decision_id), 0) AS high_water FROM kx.source_family_decisions"
            )
            high_water = int(cast(int, one_row(cursor)["high_water"]))
        return {
            "quotes": quotes,
            "concepts": concepts,
            "statements": statements,
            "ideas": ideas,
            "statement_evidence": statement_evidence,
            "wiki": [dict(wiki)] if wiki else [],
            "graph": [dict(graph)] if graph else [],
            "high_water": [{"value": high_water}],
        }

    def compose_release(self) -> ReleaseComposition:
        with self.connect() as connection:
            self.require_schema(connection)
            rows = self._slice_rows(connection)
        return compose(
            quotes=rows["quotes"],
            concepts=rows["concepts"],
            statements=rows["statements"],
            ideas=rows["ideas"],
            wiki_snapshot_id=(str(rows["wiki"][0]["snapshot_id"]) if rows["wiki"] else None),
            graph_snapshot_id=(
                str(rows["graph"][0]["graph_snapshot_id"]) if rows["graph"] else None
            ),
            family_decision_high_water=int(rows["high_water"][0]["value"]),
        )

    def build_release(self, *, built_by: str, notes: str | None = None) -> dict[str, Any]:
        """Project the confirmed slice into `kb`. Publishes nothing.

        Content-addressed: the same store builds the same release id, so building
        twice records nothing twice and a release identifier means one thing
        forever.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            rows = self._slice_rows(connection)
            composition = compose(
                quotes=rows["quotes"],
                concepts=rows["concepts"],
                statements=rows["statements"],
                ideas=rows["ideas"],
                wiki_snapshot_id=(str(rows["wiki"][0]["snapshot_id"]) if rows["wiki"] else None),
                graph_snapshot_id=(
                    str(rows["graph"][0]["graph_snapshot_id"]) if rows["graph"] else None
                ),
                family_decision_high_water=int(rows["high_water"][0]["value"]),
            )
            release_id = composition.release_id
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT built_at FROM kx.knowledge_releases WHERE release_id = %s",
                    (release_id,),
                )
                if cursor.fetchone() is not None:
                    return {**composition.as_json(), "alreadyBuilt": True}

                cursor.execute(
                    """
                    INSERT INTO kx.knowledge_releases
                        (release_id, built_by, wiki_snapshot_id, graph_snapshot_id,
                         family_decision_high_water, quote_count, concept_count,
                         statement_count, idea_count, state_sha256, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        release_id,
                        built_by,
                        composition.wiki_snapshot_id,
                        composition.graph_snapshot_id,
                        composition.family_decision_high_water,
                        composition.count("quote"),
                        composition.count("concept"),
                        composition.count("statement"),
                        composition.count("idea"),
                        composition.state_sha256,
                        notes,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO kb.releases
                        (release_id, built_at, state_sha256, quote_count, concept_count,
                         statement_count, idea_count)
                    VALUES (%s, clock_timestamp(), %s, %s, %s, %s, %s)
                    """,
                    (
                        release_id,
                        composition.state_sha256,
                        composition.count("quote"),
                        composition.count("concept"),
                        composition.count("statement"),
                        composition.count("idea"),
                    ),
                )
                for row in rows["concepts"]:
                    cursor.execute(
                        "INSERT INTO kb.concepts (release_id, concept_id, relative_path,"
                        " title, language, body) VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            release_id,
                            row["concept_id"],
                            row["relative_path"],
                            row["title"],
                            row["language"],
                            row["body"],
                        ),
                    )
                for row in rows["quotes"]:
                    cursor.execute(
                        "INSERT INTO kb.quotes (release_id, quote_id, original_text,"
                        " translated_text, translation_is_machine, attribution, source_url,"
                        " caveat, published_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            release_id,
                            row["quote_id"],
                            row["original_text"],
                            row["translated_text"],
                            row["translation_is_machine"],
                            row["attribution"],
                            row["source_url"],
                            row["caveat"],
                            row["published_at"],
                        ),
                    )
                for row in rows["statements"]:
                    cursor.execute(
                        "INSERT INTO kb.statements (release_id, statement_id, concept_id,"
                        " statement, claim_nature, confirmed_evidence)"
                        " VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            release_id,
                            row["statement_id"],
                            row["concept_id"],
                            row["statement"],
                            row["claim_nature"],
                            row["confirmed_evidence"],
                        ),
                    )
                for row in rows["statement_evidence"]:
                    cursor.execute(
                        "INSERT INTO kb.statement_evidence (release_id, statement_id,"
                        " quote_id, membership_class) VALUES (%s, %s, %s, %s)"
                        " ON CONFLICT DO NOTHING",
                        (
                            release_id,
                            row["statement_id"],
                            row["quote_id"],
                            row["membership_class"],
                        ),
                    )
                for row in rows["ideas"]:
                    cursor.execute(
                        "INSERT INTO kb.ideas (release_id, idea_id, title, statement,"
                        " independent_sources) VALUES (%s, %s, %s, %s, %s)",
                        (
                            release_id,
                            row["idea_id"],
                            row["title"],
                            row["statement"],
                            row["independent_sources"],
                        ),
                    )
                cursor.execute(
                    "INSERT INTO kx.knowledge_release_events"
                    " (release_id, action, actor, rationale) VALUES (%s, 'built', %s, %s)",
                    (release_id, built_by, notes or "built from the confirmed slice"),
                )
        return {**composition.as_json(), "alreadyBuilt": False}

    def publish_release(self, release_id: str, *, actor: str, rationale: str) -> dict[str, Any]:
        """Move the active pointer. One UPDATE, one transaction, one event."""
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT release_id FROM kb.releases WHERE release_id = %s", (release_id,)
                )
                if cursor.fetchone() is None:
                    raise ReleaseError(f"{release_id} has not been built")
                cursor.execute("SELECT release_id FROM kb.active_release")
                previous = cursor.fetchone()
                previous_id = str(previous["release_id"]) if previous else None
                if previous_id == release_id:
                    return {"releaseId": release_id, "alreadyActive": True}
                cursor.execute(
                    """
                    INSERT INTO kb.active_release (only_row, release_id, switched_by)
                    VALUES (true, %s, %s)
                    ON CONFLICT (only_row) DO UPDATE
                        SET release_id = EXCLUDED.release_id,
                            switched_at = clock_timestamp(),
                            switched_by = EXCLUDED.switched_by
                    """,
                    (release_id, actor),
                )
                cursor.execute(
                    "INSERT INTO kx.knowledge_release_events"
                    " (release_id, action, previous_release_id, actor, rationale)"
                    " VALUES (%s, 'published', %s, %s, %s)",
                    (release_id, previous_id, actor, rationale),
                )
                if previous_id is not None:
                    cursor.execute(
                        "INSERT INTO kx.knowledge_release_events"
                        " (release_id, action, previous_release_id, actor, rationale)"
                        " VALUES (%s, 'superseded', %s, %s, %s)",
                        (previous_id, release_id, actor, rationale),
                    )
        return {
            "releaseId": release_id,
            "previousReleaseId": previous_id,
            "alreadyActive": False,
        }

    def rollback_release(self, *, actor: str, rationale: str) -> dict[str, Any]:
        """Move the pointer back to whatever was active before the last publish.

        Rolling back is not a special mechanism: it is the same pointer moving the
        other way, recorded like any other event. A rollback that left no trace
        would make the event log a story about what was meant to happen.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT release_id, previous_release_id FROM kx.knowledge_release_events"
                    " WHERE action = 'published' ORDER BY event_id DESC LIMIT 1"
                )
                last = cursor.fetchone()
            if last is None or last["previous_release_id"] is None:
                raise ReleaseError("there is no earlier release to roll back to")
            target = str(last["previous_release_id"])
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kb.active_release SET release_id = %s,"
                    " switched_at = clock_timestamp(), switched_by = %s",
                    (target, actor),
                )
                cursor.execute(
                    "INSERT INTO kx.knowledge_release_events"
                    " (release_id, action, previous_release_id, actor, rationale)"
                    " VALUES (%s, 'rolled_back', %s, %s, %s)",
                    (target, str(last["release_id"]), actor, rationale),
                )
        return {"releaseId": target, "rolledBackFrom": str(last["release_id"])}

    def active_release(self) -> dict[str, Any] | None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT active.release_id, active.switched_at, active.switched_by,"
                " releases.state_sha256, releases.quote_count, releases.concept_count,"
                " releases.statement_count, releases.idea_count"
                " FROM kb.active_release AS active"
                " JOIN kb.releases AS releases USING (release_id)"
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def reconcile_release(self) -> dict[str, Any]:
        """Compare the active slice with what the store would build now."""
        active = self.active_release()
        if active is None:
            raise ReleaseError("no release is active")
        release_id = str(active["release_id"])
        current = {
            f"{element.kind}:{element.element_id}": element.fingerprint
            for element in self.compose_release().elements
        }
        with self.connect() as connection, connection.cursor() as cursor:
            published: dict[str, str] = {}
            cursor.execute(
                "SELECT quote_id, original_text, coalesce(translated_text, '') AS translated,"
                " attribution, coalesce(caveat, '') AS caveat"
                " FROM kb.quotes WHERE release_id = %s",
                (release_id,),
            )
            for row in cursor.fetchall():
                published[f"quote:{row['quote_id']}"] = sha256_bytes(
                    "\n".join(
                        (
                            str(row["original_text"]),
                            str(row["translated"]),
                            str(row["attribution"]),
                            str(row["caveat"]),
                        )
                    ).encode("utf-8")
                )
            cursor.execute(
                "SELECT statement_id, statement, confirmed_evidence FROM kb.statements"
                " WHERE release_id = %s",
                (release_id,),
            )
            for row in cursor.fetchall():
                published[f"statement:{row['statement_id']}"] = sha256_bytes(
                    f"{row['statement']}\n{row['confirmed_evidence']}".encode()
                )
            # Concepts and ideas too. Reading only two of the four kinds made the
            # first reconciliation report 84 elements missing from a slice that had
            # been published thirty seconds earlier - a comparison against half a
            # side is not a comparison.
            cursor.execute(
                "SELECT concept_id, body FROM kb.concepts WHERE release_id = %s",
                (release_id,),
            )
            for row in cursor.fetchall():
                published[f"concept:{row['concept_id']}"] = sha256_bytes(
                    str(row["body"]).encode("utf-8")
                )
            cursor.execute(
                "SELECT idea_id, title, statement, independent_sources FROM kb.ideas"
                " WHERE release_id = %s",
                (release_id,),
            )
            for row in cursor.fetchall():
                published[f"idea:{row['idea_id']}"] = sha256_bytes(
                    f"{row['title']}\n{row['statement']}\n{row['independent_sources']}".encode()
                )
        return reconcile(release_id, active=True, published=published, current=current).as_json()

    # ---------------------------------------------------------------------
    # The editor's queue (slice 2.12, ADR-0006 §3)
    # ---------------------------------------------------------------------

    def evidence_queue(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Proposed bindings a person has not decided on, most plausible first.

        Grouped by statement, because the decision a reviewer makes is about a
        statement: "which of these, if any, is what this sentence rests on". A flat
        list of 2 769 proposals is the same information arranged so nobody can act
        on it.

        Ordered by **term coverage** - the share of the statement's content words
        that appear in the quotation - and not by the retrieval score. Reciprocal
        rank fusion saturates: anything that ranked first in both languages scores
        2/61, so on the first production queue every one of the top proposals sat
        at 0.0328 and the order was effectively arbitrary. A reviewer shown noise
        first stops reading.

        Open questions are left out. "Should AgPM define a fourth level?" is not a
        statement anything can be evidence for, and nine of them were at the head
        of the queue. They are still counted as statements without evidence, which
        is correct: they do not need any (ADR-0008 §2.3).
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM kx.concept_evidence_queue WHERE claim_nature <> 'open_question'"
                )
                rows = [dict(row) for row in cursor.fetchall()]

        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            coverage = term_coverage(str(row["statement"]), str(row["quote_text"]))
            key = str(row["concept_claim_id"])
            entry = grouped.setdefault(
                key,
                {
                    "conceptClaimId": key,
                    "statement": row["statement"],
                    "claimNature": row["claim_nature"],
                    "page": row["relative_path"],
                    "conceptTitle": row["concept_title"],
                    "proposals": [],
                },
            )
            entry["proposals"].append(
                {
                    "claimId": str(row["claim_id"]),
                    "relevance": float(cast(float, row["relevance"] or 0)),
                    "coverage": round(coverage, 3),
                    "membershipClass": row["membership_class"],
                    "quote": row["quote_text"],
                    "charStart": row["char_start"],
                    "charEnd": row["char_end"],
                    "sourceUrl": row["canonical_url"],
                }
            )

        for entry in grouped.values():
            entry["proposals"].sort(key=lambda item: -float(item["coverage"]))
            # Six is what a person will actually read before deciding. The rest of
            # a statement's proposals stay in the queue and come back if these are
            # all rejected.
            entry["proposals"] = entry["proposals"][:6]
        ordered = sorted(
            grouped.values(),
            key=lambda entry: -max(float(item["coverage"]) for item in entry["proposals"]),
        )
        return {
            "statementsWaiting": len(grouped),
            "proposalsWaiting": len(rows),
            "offset": offset,
            "items": ordered[offset : offset + limit] if limit else [],
        }

    def decide_binding(
        self,
        *,
        concept_claim_id: str,
        claim_id: str,
        verdict: str,
        actor: str,
        scope: str = "editor",
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Confirm or reject one proposed binding, and record who did.

        The journal and the column are written in one transaction. The journal is
        the record; the column is the projection a queue can be drawn from without
        replaying it.
        """
        if verdict not in {"confirmed", "rejected"}:
            raise ValueError("verdict must be confirmed or rejected")
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                column = "confirmed" if verdict == "confirmed" else "rejected"
                cursor.execute(
                    f"UPDATE kx.concept_evidence"  # noqa: S608 - `column` is one of two literals
                    f" SET {column}_at = clock_timestamp(), {column}_by = %s"
                    f" WHERE concept_claim_id = %s AND claim_id = %s"
                    f"   AND confirmed_at IS NULL AND rejected_at IS NULL",
                    (actor, concept_claim_id, claim_id),
                )
                if not cursor.rowcount:
                    raise ValueError("that binding is not waiting for a decision")
                cursor.execute(
                    "INSERT INTO kx.editorial_decisions"
                    " (object_kind, object_key, verdict, actor, scope, rationale)"
                    " VALUES ('concept_evidence', %s, %s, %s, %s, %s)",
                    (f"{concept_claim_id}/{claim_id}", verdict, actor, scope, rationale),
                )
        return {"conceptClaimId": concept_claim_id, "claimId": claim_id, "verdict": verdict}

    def editorial_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT object_kind, object_key, verdict, actor, scope, decided_at, rationale"
                " FROM kx.editorial_decisions ORDER BY decision_id DESC LIMIT %s",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ---------------------------------------------------------------------
    # Research answers (slice 2.14, ADR-0004)
    # ---------------------------------------------------------------------

    def evidence_for_question(
        self,
        question: str,
        *,
        scope: str = "historical",
        limit: int = 8,
        question_vector: str | None = None,
        filters: Mapping[str, str | None] | None = None,
    ) -> tuple[EvidenceElement, ...]:
        """Retrieve numbered evidence for a question. No model decides anything here.

        Three arms - Russian words, English words, and meaning - fused by
        reciprocal rank, then filtered by what the reading pass determined. The
        semantic arm needs the question as a vector, which needs torch, which
        lives only in the embedder runtime: without it the search is exactly the
        lexical one it was before, and every hit says which arms found it.
        """
        unknown = set(filters or ()) - set(FILTERS)
        if unknown:
            raise ValueError(f"unknown filters {sorted(unknown)}; expected {list(FILTERS)}")
        parameters: dict[str, Any] = {
            "question": question,
            "rrf_k": RRF_K,
            "limit": limit * 3,
            "question_vector": question_vector,
            "embedding_model": DEFAULT_MODEL,
            "semantic_depth": SEMANTIC_DEPTH,
            **{name: (filters or {}).get(name) for name in FILTERS},
        }
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(evidence_sql(scope), parameters)
                return build_package([dict(row) for row in cursor.fetchall()], size=limit)

    def record_answer(
        self,
        *,
        question: str,
        scope: str,
        mode: str,
        package: Sequence[EvidenceElement],
        answer_text: str | None = None,
        refusal: Refusal | None = None,
        verification: Verification | None = None,
        model: str | None = None,
        prompt_sha256: str | None = None,
        answered_by: str,
        release_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one answer or one refusal, under the cache key ADR-0006 §10 fixes."""
        if (answer_text is None) == (refusal is None):
            raise ValueError("an answer or a refusal, never both and never neither")
        payload = verification.as_json() if verification else {"mode": mode, "passes": False}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.research_answers
                        (normalized_question, scope, release_id, question, mode, answer_text,
                         refusal_reason, adjacent_support, verification, evidence_package,
                         clause_count, bound_clause_count, model, prompt_sha256, answered_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (normalized_question, scope, coalesce(release_id, ''))
                        DO NOTHING
                    RETURNING answer_id
                    """,
                    (
                        normalize_question(question),
                        scope,
                        release_id,
                        question,
                        mode,
                        answer_text,
                        refusal.reason if refusal else None,
                        Jsonb(
                            [element.as_json() for element in refusal.adjacent] if refusal else []
                        ),
                        Jsonb(payload),
                        Jsonb([element.as_json() for element in package]),
                        len(verification.verdicts) if verification else 0,
                        verification.bound_clauses if verification else 0,
                        model,
                        prompt_sha256,
                        answered_by,
                    ),
                )
                row = cursor.fetchone()
        return {
            "answerId": str(row["answer_id"]) if row else None,
            "cached": row is None,
            "refusal": refusal.reason if refusal else None,
            "verification": payload,
        }

    def cached_answer(
        self, question: str, *, scope: str, release_id: str | None = None
    ) -> dict[str, Any] | None:
        """Read an answer by the key ADR-0006 §10 fixes: question, scope, release."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT answer_id, question, answer_text, refusal_reason, adjacent_support,"
                " verification, evidence_package, answered_at"
                " FROM kx.research_answers"
                " WHERE normalized_question = %s AND scope = %s"
                "   AND coalesce(release_id, '') = coalesce(%s, '')",
                (normalize_question(question), scope, release_id),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------------
    # The other queues the editor shows (slice 2.12, extended)
    # ---------------------------------------------------------------------

    def pending_family_proposals(self, *, limit: int = 25) -> dict[str, Any]:
        """Domains with documents and no confirmed family yet.

        Read from the store rather than from the batch file, so the queue is about
        what is actually unassigned and not about what a file happened to say.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT documents.canonical_url
                    FROM kx.documents
                    LEFT JOIN kx.document_source_family_current AS current
                           ON current.document_id = documents.document_id
                    JOIN kx.issue_perimeter_members AS members
                      ON members.document_id = documents.document_id
                    WHERE current.document_id IS NULL
                    GROUP BY documents.canonical_url
                    """
                )
                urls = [str(row["canonical_url"]) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT object_key FROM kx.editorial_decisions"
                    " WHERE object_kind = 'source_family' AND verdict = 'rejected'"
                )
                refused = {str(row["object_key"]) for row in cursor.fetchall()}
        proposals = propose_families(
            [DocumentHost(document_id="", canonical_url=url) for url in urls]
        )
        waiting = [
            proposal
            for proposal in proposals
            # Every unassigned domain, not only the grouped ones: a family of one
            # is still the difference between "this source" and "unknown", and
            # unknown never satisfies a two-independent-sources requirement.
            if proposal.family_key not in refused
        ]
        return {
            "waiting": len(waiting),
            "items": [
                {
                    "familyKey": proposal.family_key,
                    "displayName": proposal.display_name,
                    "domain": proposal.domain,
                    "hosts": list(proposal.hosts),
                    "documentCount": len(proposal.document_ids),
                }
                for proposal in waiting[:limit]
            ],
        }

    def decide_family_proposal(
        self, *, family_key: str, verdict: str, actor: str
    ) -> dict[str, Any]:
        """Confirm a proposed family, or record that it is not one.

        Confirming assigns every currently unassigned document of that domain. A
        document assigned later gets no family from this decision, which is
        correct: the decision was about the documents it was shown.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT documents.document_id, documents.canonical_url
                    FROM kx.documents
                    LEFT JOIN kx.document_source_family_current AS current
                           ON current.document_id = documents.document_id
                    JOIN kx.issue_perimeter_members AS members
                      ON members.document_id = documents.document_id
                    WHERE current.document_id IS NULL
                    GROUP BY documents.document_id, documents.canonical_url
                    """
                )
                candidates = [
                    DocumentHost(
                        document_id=str(row["document_id"]),
                        canonical_url=str(row["canonical_url"]),
                    )
                    for row in cursor.fetchall()
                ]
        matching = [
            proposal
            for proposal in propose_families(candidates)
            if proposal.family_key == family_key
        ]
        if verdict == "confirmed" and matching:
            proposal = matching[0]
            self.apply_family_batch(
                decided_by=actor,
                decisions=[
                    FamilyDecision(
                        family_key=proposal.family_key,
                        display_name=proposal.display_name,
                        family_kind="owner",
                        action="confirmed",
                        rationale=(
                            f"confirmed in the editor by {actor}: "
                            f"{len(proposal.document_ids)} documents on "
                            f"{', '.join(proposal.hosts)}"
                        ),
                        document_ids=proposal.document_ids,
                    )
                ],
            )
        self._record_editorial_decision(
            object_kind="source_family", object_key=family_key, verdict=verdict, actor=actor
        )
        return {"familyKey": family_key, "verdict": verdict}

    def pending_duplicate_clusters(self, *, limit: int = 25) -> tuple[int, list[dict[str, Any]]]:
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) AS total FROM kx.content_duplicate_clusters"
                    " WHERE confirmed_at IS NULL"
                )
                total = int(cast(int, one_row(cursor)["total"]))
                cursor.execute(
                    """
                    SELECT clusters.cluster_id, clusters.formation_method,
                           clusters.shingle_measure, clusters.shingle_threshold,
                           count(members.document_id) AS member_count,
                           array_agg(documents.canonical_url ORDER BY documents.canonical_url)
                               AS urls,
                           max(evidence.similarity) AS similarity
                    FROM kx.content_duplicate_clusters AS clusters
                    JOIN kx.content_duplicate_cluster_members AS members USING (cluster_id)
                    JOIN kx.documents AS documents USING (document_id)
                    LEFT JOIN kx.duplicate_evidence AS evidence USING (cluster_id)
                    WHERE clusters.confirmed_at IS NULL
                    GROUP BY clusters.cluster_id
                    ORDER BY member_count DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return total, [dict(row) for row in cursor.fetchall()]

    def decide_duplicate_cluster(
        self, *, cluster_id: str, verdict: str, actor: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                if verdict == "confirmed":
                    cursor.execute(
                        "UPDATE kx.content_duplicate_clusters"
                        " SET confirmed_at = clock_timestamp(), confirmed_by = %s"
                        " WHERE cluster_id = %s AND confirmed_at IS NULL",
                        (actor, cluster_id),
                    )
                cursor.execute(
                    "INSERT INTO kx.editorial_decisions"
                    " (object_kind, object_key, verdict, actor, scope)"
                    " VALUES ('content_duplicate_cluster', %s, %s, %s, 'editor')",
                    (cluster_id, verdict, actor),
                )
        return {"clusterId": cluster_id, "verdict": verdict}

    def pending_ideas(self, *, limit: int = 25) -> tuple[int, list[dict[str, Any]]]:
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) AS total FROM kx.ideas"
                    " WHERE admitted IS TRUE AND state = 'proposed'"
                )
                total = int(cast(int, one_row(cursor)["total"]))
                cursor.execute(
                    "SELECT idea_id, title, statement, independent_sources FROM kx.ideas"
                    " WHERE admitted IS TRUE AND state = 'proposed'"
                    " ORDER BY independent_sources DESC, title LIMIT %s",
                    (limit,),
                )
                rows = [dict(row) for row in cursor.fetchall()]
                for row in rows:
                    cursor.execute(
                        "SELECT evidence.quote_text, documents.canonical_url"
                        " FROM kx.idea_evidence AS link"
                        " JOIN kx.claim_evidence AS evidence USING (claim_id)"
                        " JOIN kx.document_versions AS versions USING (version_id)"
                        " JOIN kx.documents AS documents USING (document_id)"
                        " WHERE link.idea_id = %s LIMIT 6",
                        (row["idea_id"],),
                    )
                    row["evidence"] = [
                        (str(item["quote_text"]), str(item["canonical_url"]))
                        for item in cursor.fetchall()
                    ]
        return total, rows

    def decide_idea(self, *, idea_id: str, verdict: str, actor: str) -> dict[str, Any]:
        state = "accepted" if verdict == "confirmed" else "rejected"
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kx.ideas SET state = %s WHERE idea_id = %s AND state = 'proposed'",
                    (state, idea_id),
                )
                if not cursor.rowcount:
                    raise ValueError("that idea is not waiting for a decision")
                cursor.execute(
                    "INSERT INTO kx.idea_decisions (idea_id, verdict, decided_by, rationale)"
                    " VALUES (%s, %s, %s, %s)",
                    (idea_id, state, actor, f"decided in the editor by {actor}"),
                )
                cursor.execute(
                    "INSERT INTO kx.editorial_decisions"
                    " (object_kind, object_key, verdict, actor, scope)"
                    " VALUES ('idea', %s, %s, %s, 'editor')",
                    (idea_id, verdict, actor),
                )
        return {"ideaId": idea_id, "verdict": verdict, "state": state}

    def hosts_awaiting_policy(self, *, limit: int = 25) -> tuple[int, list[dict[str, Any]]]:
        """Hosts where a rung is known to help and no profile allows it."""
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT lower(split_part(split_part(gaps.canonical_url, '://', 2), '/', 1))
                               AS host,
                           gaps.last_error_code AS reason,
                           count(*) AS documents
                    FROM kx.acquisition_gap_queue AS gaps
                    LEFT JOIN kx.host_profiles AS profiles
                           ON profiles.host =
                              lower(split_part(split_part(gaps.canonical_url, '://', 2), '/', 1))
                    WHERE gaps.terminal_reason = 'blocked_by_host'
                      AND profiles.host IS NULL
                    GROUP BY 1, 2
                    ORDER BY documents DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT count(DISTINCT
                        lower(split_part(split_part(canonical_url, '://', 2), '/', 1)))
                        AS total
                    FROM kx.acquisition_gap_queue AS gaps
                    LEFT JOIN kx.host_profiles AS profiles
                           ON profiles.host =
                              lower(split_part(split_part(gaps.canonical_url, '://', 2), '/', 1))
                    WHERE gaps.terminal_reason = 'blocked_by_host' AND profiles.host IS NULL
                    """
                )
                total = int(cast(int, one_row(cursor)["total"]))
        for row in rows:
            row["would_help"] = ESCALATION_HINT.get(str(row["reason"]), "network")
        return total, rows

    def decide_host_policy(self, *, host: str, verdict: str, actor: str) -> dict[str, Any]:
        """Write a host profile, or record that the host is left as it is."""
        if verdict == "confirmed":
            total, rows = self.hosts_awaiting_policy(limit=500)
            match = next((row for row in rows if str(row["host"]) == host), None)
            rung = str(match["would_help"]) if match else "network_browser_headers"
            reason = str(match["reason"]) if match else "unknown"
            self.write_host_profile(
                HostProfile(
                    host=host,
                    rungs=("network", rung),
                    robots_policy=(
                        "override_recorded" if rung == "network_robots_override" else "respect"
                    ),
                    rationale=(
                        f"decided in the editor by {actor}: {match['documents'] if match else 0}"
                        f" documents blocked with {reason}; {rung} is the rung that would help"
                    ),
                    decided_by=actor,
                )
            )
        self._record_editorial_decision(
            object_kind="host_profile", object_key=host, verdict=verdict, actor=actor
        )
        return {"host": host, "verdict": verdict}

    def pending_alias_proposals(self, *, limit: int = 25) -> tuple[int, list[dict[str, Any]]]:
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) AS total FROM kx.entity_alias_proposals"
                    " WHERE decided_at IS NULL"
                )
                total = int(cast(int, one_row(cursor)["total"]))
                cursor.execute(
                    "SELECT proposal_id, original_form, proposed_form, language, occurrences"
                    " FROM kx.entity_alias_proposals WHERE decided_at IS NULL"
                    " ORDER BY occurrences DESC LIMIT %s",
                    (limit,),
                )
                return total, [dict(row) for row in cursor.fetchall()]

    def decide_alias_proposal(
        self, *, proposal_id: int, verdict: str, actor: str
    ) -> dict[str, Any]:
        decision = "accepted" if verdict == "confirmed" else "rejected"
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kx.entity_alias_proposals SET decided_at = clock_timestamp(),"
                    " decided_by = %s, decision = %s WHERE proposal_id = %s"
                    "   AND decided_at IS NULL",
                    (actor, decision, proposal_id),
                )
                if not cursor.rowcount:
                    raise ValueError("that proposal is not waiting for a decision")
                cursor.execute(
                    "INSERT INTO kx.editorial_decisions"
                    " (object_kind, object_key, verdict, actor, scope)"
                    " VALUES ('entity_alias', %s, %s, %s, 'editor')",
                    (str(proposal_id), verdict, actor),
                )
        return {"proposalId": proposal_id, "verdict": verdict}

    def _record_editorial_decision(
        self, *, object_kind: str, object_key: str, verdict: str, actor: str
    ) -> None:
        with self.connect() as connection, connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO kx.editorial_decisions"
                " (object_kind, object_key, verdict, actor, scope)"
                " VALUES (%s, %s, %s, %s, 'editor')",
                (object_kind, object_key, verdict, actor),
            )

    # ---------------------------------------------------------------------
    # The topic skeleton (slice 2.5в)
    # ---------------------------------------------------------------------

    def skeleton_candidates(self) -> tuple[SkeletonCandidate, ...]:
        """The backbones that exist today, read out of the stored wiki snapshot.

        Out of the store rather than off a filesystem, so what the owner is shown
        is what the base actually holds.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT ON (concepts.relative_path)"
                    " concepts.relative_path, versions.body"
                    " FROM kx.concept_versions AS versions"
                    " JOIN kx.concepts AS concepts USING (concept_id)"
                    " ORDER BY concepts.relative_path, versions.imported_at DESC"
                )
                rows = [dict(row) for row in cursor.fetchall()]
        pages = {str(row["relative_path"]): str(row["body"]) for row in rows}
        counts: dict[str, int] = {}
        for path in pages:
            parts = path.split("/")
            if len(parts) >= 3 and parts[0] == "wiki":
                counts[parts[1]] = counts.get(parts[1], 0) + 1
        # The five directories the model declares and has no pages for are the
        # finding, so they have to appear with a zero rather than be absent.
        for empty in ("data", "market", "maturity", "open-questions", "risks"):
            counts.setdefault(empty, 0)
        return skeleton_candidates(pages=pages, section_counts=counts)

    def accepted_skeleton(self) -> list[dict[str, Any]]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT topic_key, title, source, level, state, description"
                " FROM kx.topics ORDER BY source, level, title"
            )
            return [dict(row) for row in cursor.fetchall()]

    def adopt_skeleton(self, source: str, *, actor: str) -> dict[str, Any]:
        """Turn one candidate into the topic table. Nothing else changes yet."""
        chosen = next((item for item in self.skeleton_candidates() if item.source == source), None)
        if chosen is None:
            raise ValueError(f"no skeleton candidate named {source!r}")
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for element in chosen.elements:
                    cursor.execute(
                        """
                        INSERT INTO kx.topics
                            (topic_key, title, source, level, description, state, created_by)
                        VALUES (%s, %s, %s, 1, %s, 'accepted', %s)
                        ON CONFLICT (topic_key) DO NOTHING
                        """,
                        (
                            element.topic_key,
                            element.title,
                            chosen.source,
                            element.description or None,
                            actor,
                        ),
                    )
                cursor.execute(
                    "INSERT INTO kx.editorial_decisions"
                    " (object_kind, object_key, verdict, actor, scope, rationale)"
                    " VALUES ('topic_skeleton', %s, 'confirmed', %s, 'editor', %s)",
                    (source, actor, f"adopted as the backbone: {chosen.title}"),
                )
        return {"source": source, "topics": len(chosen.elements)}

    def adopt_authored_skeleton(self, skeleton: AuthoredSkeleton) -> dict[str, Any]:
        """Write the owner's own composition into the topic table.

        Parents come before their children in the flattened list, so one pass can
        resolve `parent_key` against what it has already inserted. A key that is
        already there is left alone: reloading a corrected file must not create a
        second topic meaning the same thing.
        """
        inserted = 0
        parents: dict[str, str] = {}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for topic in skeleton.topics:
                    parent_id = parents.get(topic.parent_key) if topic.parent_key else None
                    if topic.parent_key and parent_id is None:
                        raise SkeletonError(
                            f"{topic.key}: parent {topic.parent_key} was not inserted first"
                        )
                    cursor.execute(
                        """
                        INSERT INTO kx.topics
                            (topic_key, title, source, level, parent_id, description,
                             state, created_by)
                        VALUES (%s, %s, 'authored', %s, %s, %s, 'accepted', %s)
                        ON CONFLICT (topic_key) DO NOTHING
                        RETURNING topic_id
                        """,
                        (
                            topic.key,
                            topic.title,
                            topic.level,
                            parent_id,
                            topic.description or None,
                            skeleton.decided_by,
                        ),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        inserted += 1
                        parents[topic.key] = str(row["topic_id"])
                        continue
                    cursor.execute(
                        "SELECT topic_id FROM kx.topics WHERE topic_key = %s", (topic.key,)
                    )
                    parents[topic.key] = str(one_row(cursor)["topic_id"])
                cursor.execute(
                    "INSERT INTO kx.editorial_decisions"
                    " (object_kind, object_key, verdict, actor, scope, rationale)"
                    " VALUES ('topic_skeleton', %s, 'confirmed', %s, 'editor', %s)",
                    (
                        skeleton.skeleton_key,
                        skeleton.decided_by,
                        f"authored backbone adopted: {skeleton.title}",
                    ),
                )
        return {
            "skeletonKey": skeleton.skeleton_key,
            "decidedBy": skeleton.decided_by,
            "topics": len(skeleton.topics),
            "inserted": inserted,
            "byLevel": skeleton.by_level(),
        }

    def topics(self, *, level: int | None = None) -> list[dict[str, Any]]:
        """The accepted backbone, each topic carrying the path down to it."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH RECURSIVE walk AS (
                    SELECT topic_id, topic_key, title, level, parent_id, description,
                           title::text AS path
                    FROM kx.topics WHERE parent_id IS NULL AND state = 'accepted'
                    UNION ALL
                    SELECT child.topic_id, child.topic_key, child.title, child.level,
                           child.parent_id, child.description,
                           walk.path || ' / ' || child.title
                    FROM kx.topics AS child
                    JOIN walk ON walk.topic_id = child.parent_id
                    WHERE child.state = 'accepted'
                )
                SELECT topic_id::text AS topic_id, topic_key, title, level, description, path
                FROM walk
                WHERE %s::smallint IS NULL OR level = %s::smallint
                ORDER BY path
                """,
                (level, level),
            )
            return [dict(row) for row in cursor.fetchall()]

    def decide_skeleton(self, *, source: str, verdict: str, actor: str) -> dict[str, Any]:
        if verdict == "confirmed":
            return self.adopt_skeleton(source, actor=actor)
        self._record_editorial_decision(
            object_kind="topic_skeleton", object_key=source, verdict=verdict, actor=actor
        )
        return {"source": source, "verdict": verdict}

    def unassigned_topic_items(self, kind: str, *, limit: int = 500) -> list[AssignableItem]:
        """What still has no subject: wiki statements, or documents that hold evidence.

        Only documents that actually carry an exact quotation are offered. The
        other eight thousand cannot be reached by a binding either way, and
        classifying them would be a model bill for nothing.
        """
        if kind not in ("statement", "document"):
            raise ValueError("kind must be 'statement' or 'document'")
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                if kind == "statement":
                    cursor.execute(
                        """
                        SELECT claims.concept_claim_id::text AS key, claims.statement AS text
                        FROM kx.concept_claims AS claims
                        WHERE claims.claim_nature <> 'open_question'
                          AND NOT EXISTS (
                              SELECT 1 FROM kx.concept_claim_topics AS assigned
                              WHERE assigned.concept_claim_id = claims.concept_claim_id
                          )
                        ORDER BY claims.concept_claim_id
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    return [
                        AssignableItem(key=str(row["key"]), text=" ".join(str(row["text"]).split()))
                        for row in cursor.fetchall()
                    ]
                cursor.execute(
                    """
                    SELECT DISTINCT ON (versions.document_id)
                           versions.document_id AS key,
                           versions.title,
                           left(versions.canonical_text, %s) AS lede
                    FROM kx.document_versions AS versions
                    WHERE versions.is_complete
                      AND EXISTS (
                          SELECT 1 FROM kx.claim_evidence AS evidence
                          WHERE evidence.version_id = versions.version_id
                            AND evidence.match_status = 'exact'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM kx.document_topics AS assigned
                          WHERE assigned.document_id = versions.document_id
                      )
                    ORDER BY versions.document_id, versions.fetched_at DESC
                    LIMIT %s
                    """,
                    (LEDE_CHARS * 3, limit),
                )
                return [
                    document_item(
                        document_id=str(row["key"]),
                        title=str(row["title"] or ""),
                        lede=str(row["lede"] or ""),
                    )
                    for row in cursor.fetchall()
                ]

    def record_topic_assignments(
        self,
        kind: str,
        assignments: Sequence[Assignment],
        *,
        assigned_by: str,
        method: str = "model",
    ) -> int:
        """Write one batch of subjects. A key the backbone does not have is refused."""
        if kind not in ("statement", "document"):
            raise ValueError("kind must be 'statement' or 'document'")
        table = "concept_claim_topics" if kind == "statement" else "document_topics"
        column = "concept_claim_id" if kind == "statement" else "document_id"
        written = 0
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for assignment in assignments:
                    for topic_key in assignment.topic_keys:
                        cursor.execute(
                            f"""
                            INSERT INTO kx.{table} ({column}, topic_id, assigned_by, method)
                            SELECT %s, topics.topic_id, %s, %s
                            FROM kx.topics AS topics
                            WHERE topics.topic_key = %s AND topics.state = 'accepted'
                            ON CONFLICT DO NOTHING
                            """,  # noqa: S608 - both names come from the branch above
                            (assignment.key, assigned_by, method, topic_key),
                        )
                        written += cursor.rowcount
        return written

    def topic_assignment_report(self) -> dict[str, Any]:
        """How much of each side now has a subject, and which subjects they are."""
        with self.connect() as connection, connection.cursor() as cursor:
            # Counted without joining the assignments: a statement placed under
            # three subjects is one statement, and a LEFT JOIN would report 448 of
            # 233. Every count here is over its own population.
            cursor.execute(
                """
                SELECT count(*) AS statements,
                       count(*) FILTER (
                           WHERE EXISTS (
                               SELECT 1 FROM kx.concept_claim_topics AS assigned
                               WHERE assigned.concept_claim_id = claims.concept_claim_id
                           )
                       ) AS statements_placed
                FROM kx.concept_claims AS claims
                WHERE claims.claim_nature <> 'open_question'
                """
            )
            statements = dict(one_row(cursor))
            cursor.execute(
                """
                WITH holders AS (
                    SELECT DISTINCT versions.document_id
                    FROM kx.claim_evidence AS evidence
                    JOIN kx.document_versions AS versions USING (version_id)
                    WHERE evidence.match_status = 'exact'
                )
                SELECT count(*) AS documents,
                       count(*) FILTER (
                           WHERE EXISTS (
                               SELECT 1 FROM kx.document_topics AS assigned
                               WHERE assigned.document_id = holders.document_id
                           )
                       ) AS documents_placed
                FROM holders
                """
            )
            documents = dict(one_row(cursor))
            cursor.execute(
                """
                SELECT roots.topic_key, roots.title,
                       count(DISTINCT statements.concept_claim_id) AS statements,
                       count(DISTINCT documents.document_id) AS documents
                FROM kx.topics AS roots
                LEFT JOIN kx.topics AS children ON children.parent_id = roots.topic_id
                LEFT JOIN kx.concept_claim_topics AS statements
                       ON statements.topic_id IN (roots.topic_id, children.topic_id)
                LEFT JOIN kx.document_topics AS documents
                       ON documents.topic_id IN (roots.topic_id, children.topic_id)
                WHERE roots.parent_id IS NULL
                GROUP BY roots.topic_key, roots.title
                ORDER BY statements DESC, roots.topic_key
                """
            )
            per_topic = [dict(row) for row in cursor.fetchall()]
        return {**statements, **documents, "byTopic": per_topic}

    # ---------------------------------------------------------------------
    # The owner's acceptance criterion, measured rather than asserted
    # ---------------------------------------------------------------------

    def traceability_report(self, *, sample: int = 5) -> dict[str, Any]:
        """Can the base be walked in both directions, on live data?

        Her criterion, in her words: from a question to a conclusion, a statement,
        an exact fragment and a primary source - and back, from a new issue
        material to statements, concepts, links and a proposal to update the wiki.

        This counts both walks over the whole store rather than demonstrating one
        of each. A chain that holds for a hand-picked example and breaks for four
        statements in five is not traceability; the percentages are the answer.
        """
        with self.connect() as connection, connection.cursor() as cursor:
            # Forward: question -> statement -> quotation -> span -> source.
            cursor.execute(
                """
                SELECT count(DISTINCT claims.claim_id) AS statements,
                       count(DISTINCT claims.claim_id)
                           FILTER (WHERE evidence.claim_id IS NOT NULL) AS with_a_quotation,
                       count(DISTINCT claims.claim_id) FILTER (
                           WHERE substr(versions.canonical_text, evidence.char_start + 1,
                                        evidence.char_end - evidence.char_start)
                                 = evidence.quote_text
                       ) AS quotation_reproduces,
                       count(DISTINCT claims.claim_id)
                           FILTER (WHERE documents.canonical_url <> '') AS with_a_source,
                       count(DISTINCT claims.claim_id)
                           FILTER (WHERE reading.claim_id IS NOT NULL) AS read,
                       count(DISTINCT claims.claim_id)
                           FILTER (WHERE dates.document_id IS NOT NULL) AS dated,
                       -- `claim_evidence` is keyed by (claim, version, start, end), so
                       -- more than one exact quotation per statement is legal. None
                       -- exist today, and five queries elsewhere join on the claim
                       -- alone and would each silently multiply their rows if one
                       -- appeared. Counted here so it is seen rather than discovered.
                       count(*) - count(DISTINCT claims.claim_id)
                           AS quotations_beyond_the_first
                FROM kx.claims AS claims
                -- LEFT, and the whole point of the measurement. Under an inner
                -- join `with_a_quotation` was tautologically equal to
                -- `statements`: a statement with no exact quotation left the
                -- numerator and the denominator together, so the one failure this
                -- report exists to catch was the one it could never show.
                LEFT JOIN kx.claim_evidence AS evidence
                  ON evidence.claim_id = claims.claim_id AND evidence.match_status = 'exact'
                LEFT JOIN kx.document_versions AS versions
                       ON versions.version_id = claims.version_id
                LEFT JOIN kx.documents AS documents
                       ON documents.document_id = versions.document_id
                LEFT JOIN kx.claim_reading AS reading ON reading.claim_id = claims.claim_id
                LEFT JOIN kx.document_dates AS dates ON dates.document_id = documents.document_id
                """
            )
            forward = dict(one_row(cursor))

            # Back: issue material -> statements -> subjects -> links -> a page.
            cursor.execute(
                """
                WITH materials AS (
                    SELECT DISTINCT members.document_id, members.issue_date, members.title
                    FROM kx.issue_perimeter_members AS members
                ),
                walked AS (
                    SELECT materials.document_id,
                           materials.issue_date,
                           materials.title,
                           count(DISTINCT claims.claim_id) AS statements,
                           count(DISTINCT placed.topic_id) AS subjects,
                           count(DISTINCT links.to_id) AS linked,
                           count(DISTINCT pages.concept_id) AS pages
                    FROM materials
                    LEFT JOIN kx.document_versions AS versions
                           ON versions.document_id = materials.document_id
                    LEFT JOIN kx.claims AS claims ON claims.version_id = versions.version_id
                    LEFT JOIN kx.claim_topics AS placed ON placed.claim_id = claims.claim_id
                    LEFT JOIN kx.knowledge_links AS links
                           ON links.from_kind = 'claim' AND links.from_id = claims.claim_id
                    LEFT JOIN kx.concept_claim_topics AS page_topics
                           ON page_topics.topic_id = placed.topic_id
                    LEFT JOIN kx.concept_claims AS page_claims
                           ON page_claims.concept_claim_id = page_topics.concept_claim_id
                    LEFT JOIN kx.concept_versions AS pages
                           ON pages.concept_version_id = page_claims.concept_version_id
                    GROUP BY 1, 2, 3
                )
                SELECT count(*) AS materials,
                       count(*) FILTER (WHERE statements > 0) AS reach_statements,
                       count(*) FILTER (WHERE subjects > 0) AS reach_subjects,
                       count(*) FILTER (WHERE linked > 0) AS reach_links,
                       count(*) FILTER (WHERE pages > 0) AS reach_a_page
                FROM walked
                """
            )
            backward = dict(one_row(cursor))

            # One worked example of each, so the numbers can be checked by hand.
            cursor.execute(
                """
                SELECT claims.claim_id, claims.normalized_text, evidence.quote_text,
                       evidence.char_start, evidence.char_end, documents.canonical_url,
                       reading.material_kind, status.status
                FROM kx.claims AS claims
                JOIN kx.claim_evidence AS evidence
                  ON evidence.claim_id = claims.claim_id AND evidence.match_status = 'exact'
                JOIN kx.document_versions AS versions ON versions.version_id = claims.version_id
                JOIN kx.documents AS documents ON documents.document_id = versions.document_id
                JOIN kx.claim_reading AS reading ON reading.claim_id = claims.claim_id
                JOIN kx.knowledge_status_current AS status
                  ON status.unit_kind = 'claim' AND status.unit_id = claims.claim_id
                WHERE EXISTS (
                    SELECT 1 FROM kx.claim_topics WHERE claim_id = claims.claim_id
                )
                ORDER BY claims.claim_id
                LIMIT %s
                """,
                (sample,),
            )
            examples = [dict(row) for row in cursor.fetchall()]
        return {"forward": forward, "back": backward, "examples": examples}

    def wiki_suggestions(self, *, limit: int = 25) -> tuple[int, list[dict[str, Any]]]:
        """The best few store statements for each statement on her pages (UC-09).

        Shortlisted by both methods, exactly as links are. The subject is the
        lexical side - a model read the words and placed both on the backbone -
        and the neighbourhood is the semantic side.

        Sharing a subject alone is a cross product: it offered 95 554 pairs, which
        is the 163-hour queue her delegation boundary exists to prevent. The
        nearest three within a sentence's distance is a queue that can be worked.

        Machine-proposed and unsigned - the wiki is authored text and stays hers
        (P4), so this is a queue rather than an edit.
        """
        query = """
            WITH page_vectors AS (
                SELECT page_claims.concept_claim_id, page_claims.statement,
                       page_claims.concept_version_id, vectors.embedding
                FROM kx.concept_claims AS page_claims
                JOIN kx.text_embeddings AS vectors
                  ON vectors.owner_kind = 'concept_claim'
                 AND vectors.owner_key = page_claims.concept_claim_id::text
                 AND vectors.model_id = %(model)s
                WHERE page_claims.claim_nature <> 'open_question'
            ),
            nearest AS (
                SELECT page.concept_claim_id,
                       page.statement AS page_statement,
                       page.concept_version_id,
                       claims.claim_id,
                       claims.normalized_text,
                       topics.title AS subject,
                       vectors.embedding <=> page.embedding AS distance,
                       row_number() OVER (
                           PARTITION BY page.concept_claim_id
                           ORDER BY vectors.embedding <=> page.embedding
                       ) AS rank
                FROM page_vectors AS page
                JOIN kx.concept_claim_topics AS page_topics
                  ON page_topics.concept_claim_id = page.concept_claim_id
                JOIN kx.claim_topics AS placed ON placed.topic_id = page_topics.topic_id
                JOIN kx.topics AS topics ON topics.topic_id = placed.topic_id
                JOIN kx.claims AS claims ON claims.claim_id = placed.claim_id
                JOIN kx.claim_reading AS reading ON reading.claim_id = claims.claim_id
                JOIN kx.text_embeddings AS vectors
                  ON vectors.owner_kind = 'claim_evidence'
                 AND vectors.owner_key = claims.claim_id::text
                 AND vectors.model_id = %(model)s
                WHERE reading.admission = 'knowledge'
                  AND topics.state = 'accepted'
                  AND vectors.embedding <=> page.embedding <= %(max_distance)s
                  AND NOT EXISTS (
                      SELECT 1 FROM kx.concept_evidence AS bound
                      WHERE bound.claim_id = claims.claim_id
                        AND bound.concept_claim_id = page.concept_claim_id
                  )
            )
            SELECT DISTINCT ON (nearest.concept_claim_id, nearest.claim_id)
                   concepts.relative_path,
                   versions.title AS page_title,
                   nearest.concept_claim_id,
                   nearest.page_statement,
                   nearest.subject,
                   nearest.claim_id,
                   nearest.normalized_text,
                   nearest.distance,
                   evidence.quote_text,
                   documents.canonical_url,
                   reading.material_kind,
                   status.status
            FROM nearest
            JOIN kx.claim_evidence AS evidence
              ON evidence.claim_id = nearest.claim_id AND evidence.match_status = 'exact'
            JOIN kx.claim_reading AS reading ON reading.claim_id = nearest.claim_id
            JOIN kx.claims AS source ON source.claim_id = nearest.claim_id
            JOIN kx.document_versions AS document_version
              ON document_version.version_id = source.version_id
            JOIN kx.documents AS documents
              ON documents.document_id = document_version.document_id
            JOIN kx.knowledge_status_current AS status
              ON status.unit_kind = 'claim' AND status.unit_id = nearest.claim_id
            JOIN kx.concept_versions AS versions
              ON versions.concept_version_id = nearest.concept_version_id
            JOIN kx.concepts AS concepts ON concepts.concept_id = versions.concept_id
            WHERE nearest.rank <= %(neighbours)s
            ORDER BY nearest.concept_claim_id, nearest.claim_id, nearest.distance
        """
        parameters: dict[str, Any] = {
            "model": DEFAULT_MODEL,
            "max_distance": LINK_MAX_DISTANCE,
            "neighbours": LINK_NEIGHBOURS,
        }
        with self.connect() as connection, connection.cursor() as cursor:
            # The wall asks every queue for its count with limit 0, so the total
            # cannot come from the page: a tab that reports what fits on screen
            # reads as empty the moment the screen is empty.
            cursor.execute(
                f"SELECT count(*) AS total FROM ({query}) AS all_of_them",  # noqa: S608
                parameters,
            )
            total = int(one_row(cursor)["total"])
            cursor.execute(f"{query} LIMIT %(limit)s", {**parameters, "limit": limit})
            return total, [dict(row) for row in cursor.fetchall()]

    # ---------------------------------------------------------------------
    # Two queues the owner's own decisions create (6: promotion, 11: expiry)
    # ---------------------------------------------------------------------

    def promotion_candidates(self, *, limit: int = 25) -> tuple[int, list[dict[str, Any]]]:
        """Statements the machine proposes moving up a rung, and what it counted.

        Decision 6: the machine proposes by a threshold of independent
        confirmations, the owner confirms. The threshold here is two *other*
        statements that support it and come from different sources - a retelling
        of the same forecast by four outlets is one observation, and the reading
        pass is what makes that visible.

        Only `observed_signal` is offered. A claim already at canon has nowhere to
        go, and one born an external reference is somebody else's standard - moving
        it is a judgement about the canon, which is hers to start, not the
        machine's to suggest.
        """
        query = """
            WITH confirmation AS (
                -- Both ends of every `supports` link, and the reason both count.
                --
                -- The judge is shown an ordered pair and answers "the second
                -- confirms the first", so its verdict looks directional. But which
                -- statement is shown first is decided by `source.claim_id <
                -- other.claim_id` - a comparison of two uuids. The order carries no
                -- evidence, so a query that reads it is counting a coin flip.
                --
                -- What the link asserts is symmetric: the same state of affairs,
                -- observed a second time from somewhere else. That makes each link
                -- one independent observation for the statement at *either* end.
                --
                -- Measured on production: the `to` side alone (what this counted
                -- before) clears the floor for 682 statements, the `from` side for
                -- 674, and only 113 are in both - the overlap is small precisely
                -- because the coin flip is independent per pair. Both sides: 1 557.
                SELECT from_id AS claim_id, to_id AS confirmer
                FROM kx.knowledge_links
                WHERE link_type = 'supports'
                  AND from_kind = 'claim' AND to_kind = 'claim'
                UNION ALL
                SELECT to_id AS claim_id, from_id AS confirmer
                FROM kx.knowledge_links
                WHERE link_type = 'supports'
                  AND from_kind = 'claim' AND to_kind = 'claim'
            ),
            support AS (
                SELECT confirmation.claim_id,
                       count(DISTINCT coalesce(
                           nullif(reading.primary_source, ''), documents.canonical_url
                       )) AS independent
                FROM confirmation
                JOIN kx.claims AS claims ON claims.claim_id = confirmation.confirmer
                JOIN kx.claim_reading AS reading
                  ON reading.claim_id = confirmation.confirmer
                JOIN kx.document_versions AS versions ON versions.version_id = claims.version_id
                JOIN kx.documents AS documents ON documents.document_id = versions.document_id
                GROUP BY 1
            ),
            candidates AS (
                SELECT claims.claim_id, claims.normalized_text, evidence.quote_text,
                       documents.canonical_url, reading.material_kind,
                       reading.primary_source, reading.is_retelling,
                       support.independent
                FROM support
                JOIN kx.claims AS claims USING (claim_id)
                JOIN kx.claim_reading AS reading USING (claim_id)
                JOIN kx.claim_evidence AS evidence
                  ON evidence.claim_id = claims.claim_id AND evidence.match_status = 'exact'
                JOIN kx.document_versions AS versions ON versions.version_id = claims.version_id
                JOIN kx.documents AS documents ON documents.document_id = versions.document_id
                JOIN kx.knowledge_status_current AS status
                  ON status.unit_kind = 'claim' AND status.unit_id = claims.claim_id
                WHERE support.independent >= %s
                  AND status.status = 'observed_signal'
                  AND reading.admission = 'knowledge'
                  AND NOT EXISTS (
                      SELECT 1 FROM kx.editorial_decisions AS decided
                      WHERE decided.object_kind = 'status_promotion'
                        AND decided.object_key = claims.claim_id::text
                  )
            )
            SELECT * FROM candidates
        """
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) AS total FROM ({query}) AS all_of_them",  # noqa: S608
                (PROMOTION_FLOOR,),
            )
            total = int(one_row(cursor)["total"])
            cursor.execute(
                f"{query} ORDER BY independent DESC, claim_id LIMIT %s",
                (PROMOTION_FLOOR, limit),
            )
            return total, [dict(row) for row in cursor.fetchall()]

    def decide_promotion(self, *, claim_id: str, verdict: str, actor: str) -> dict[str, Any]:
        """Record her decision, and - only on a yes - the status it grants.

        Both halves in one transaction: a promotion recorded without the status,
        or a status granted without the decision behind it, are each worse than
        neither. `method='manual'` is what makes it visible in
        `knowledge_status_current`; the machine's proposal never is.
        """
        if verdict not in ("confirmed", "rejected"):
            raise ValueError("verdict must be 'confirmed' or 'rejected'")
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.editorial_decisions
                        (object_kind, object_key, verdict, actor, scope)
                    VALUES ('status_promotion', %s, %s, %s, 'editor')
                    """,
                    (claim_id, verdict, actor),
                )
                granted = 0
                if verdict == "confirmed":
                    cursor.execute(
                        """
                        INSERT INTO kx.knowledge_status
                            (unit_kind, unit_id, status, method, rationale, set_by)
                        VALUES ('claim', %s, 'operationalization', 'manual',
                                'подтверждено независимыми наблюдениями, решение владельца', %s)
                        """,
                        (claim_id, actor),
                    )
                    granted = cursor.rowcount
        return {"claimId": claim_id, "verdict": verdict, "statusGranted": granted}

    def expired_statements(self, *, limit: int = 25) -> tuple[int, list[dict[str, Any]]]:
        """What has passed its freshness rule and is waiting to be looked at again.

        Decision 11: expiry queues a review and changes nothing on its own. A
        forecast about 2027 stops being a forecast when 2027 arrives, and the base
        should say so rather than quietly keep asserting it.
        """
        query = """
            SELECT claims.claim_id, claims.normalized_text, evidence.quote_text,
                   documents.canonical_url, reading.material_kind, reading.valid_until,
                   dates.shown_on, dates.shown_kind
            FROM kx.claim_reading AS reading
            JOIN kx.claims AS claims USING (claim_id)
            JOIN kx.claim_evidence AS evidence
              ON evidence.claim_id = claims.claim_id AND evidence.match_status = 'exact'
            JOIN kx.document_versions AS versions ON versions.version_id = claims.version_id
            JOIN kx.documents AS documents ON documents.document_id = versions.document_id
            LEFT JOIN kx.document_dates AS dates ON dates.document_id = documents.document_id
            WHERE reading.valid_until IS NOT NULL
              AND reading.valid_until < clock_timestamp()
              AND reading.admission <> 'rejected'
              AND NOT EXISTS (
                  SELECT 1 FROM kx.editorial_decisions AS decided
                  WHERE decided.object_kind = 'freshness_review'
                    AND decided.object_key = claims.claim_id::text
              )
        """
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) AS total FROM ({query}) AS all_of_them")  # noqa: S608
            total = int(one_row(cursor)["total"])
            cursor.execute(
                f"{query} ORDER BY reading.valid_until, claims.claim_id LIMIT %s",
                (limit,),
            )
            return total, [dict(row) for row in cursor.fetchall()]

    def decide_freshness(self, *, claim_id: str, verdict: str, actor: str) -> dict[str, Any]:
        """Her verdict on an expired statement. Recorded; nothing else moves."""
        if verdict not in ("confirmed", "rejected"):
            raise ValueError("verdict must be 'confirmed' or 'rejected'")
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.editorial_decisions
                        (object_kind, object_key, verdict, actor, scope)
                    VALUES ('freshness_review', %s, %s, %s, 'editor')
                    """,
                    (claim_id, verdict, actor),
                )
        return {"claimId": claim_id, "verdict": verdict}

    # ---------------------------------------------------------------------
    # Stage 2: what one statement does to another (owner decision 12)
    # ---------------------------------------------------------------------

    def link_candidates(
        self,
        *,
        limit: int = 4000,
        neighbours: int = LINK_NEIGHBOURS,
        max_distance: float = LINK_MAX_DISTANCE,
    ) -> list[Pair]:
        """Pairs worth a judgement: near in meaning, sharing a subject, both knowledge.

        Both methods are in this one query rather than two. The subject is the
        lexical side's job - it came from a model reading the words - and the
        neighbourhood is the semantic side's. A pair that fails either is not
        shortlisted, which is what keeps a few thousand judgements out of ninety-six
        million pairs.

        Ordered `from` before `to` by id so a pair is offered once, not twice.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH knowledge AS (
                        SELECT claims.claim_id, claims.normalized_text, vectors.embedding
                        FROM kx.claims AS claims
                        JOIN kx.claim_reading AS reading USING (claim_id)
                        JOIN kx.text_embeddings AS vectors
                          ON vectors.owner_kind = 'claim_evidence'
                         AND vectors.owner_key = claims.claim_id::text
                         AND vectors.model_id = %(model)s
                        WHERE reading.admission = 'knowledge'
                    ),
                    placed AS (
                        SELECT claim_topics.claim_id, topics.topic_key, topics.title
                        FROM kx.claim_topics
                        JOIN kx.topics AS topics USING (topic_id)
                        WHERE topics.state = 'accepted'
                    ),
                    nearest AS (
                        SELECT source.claim_id AS from_id,
                               source.normalized_text AS from_text,
                               other.claim_id AS to_id,
                               other.normalized_text AS to_text,
                               source.embedding <=> other.embedding AS distance,
                               shared.title AS shared_topic,
                               row_number() OVER (
                                   PARTITION BY source.claim_id
                                   ORDER BY source.embedding <=> other.embedding
                               ) AS rank
                        FROM knowledge AS source
                        JOIN placed AS source_topic ON source_topic.claim_id = source.claim_id
                        JOIN placed AS shared
                          ON shared.topic_key = source_topic.topic_key
                         AND shared.claim_id <> source.claim_id
                        JOIN knowledge AS other ON other.claim_id = shared.claim_id
                        WHERE source.claim_id < other.claim_id
                          AND source.embedding <=> other.embedding <= %(max_distance)s
                          AND NOT EXISTS (
                              SELECT 1 FROM kx.knowledge_links AS existing
                              WHERE existing.from_kind = 'claim'
                                AND existing.from_id = source.claim_id
                                AND existing.to_kind = 'claim'
                                AND existing.to_id = other.claim_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM kx.link_judgements AS looked
                              WHERE looked.from_id = source.claim_id
                                AND looked.to_id = other.claim_id
                          )
                    )
                    SELECT DISTINCT ON (from_id, to_id)
                           from_id, from_text, to_id, to_text, distance, shared_topic
                    FROM nearest
                    WHERE rank <= %(neighbours)s
                    ORDER BY from_id, to_id, distance
                    LIMIT %(limit)s
                    """,
                    {
                        "model": DEFAULT_MODEL,
                        "neighbours": neighbours,
                        "max_distance": max_distance,
                        "limit": limit,
                    },
                )
                rows = cursor.fetchall()
        return [
            Pair(
                from_id=str(row["from_id"]),
                to_id=str(row["to_id"]),
                from_text=str(row["from_text"]),
                to_text=str(row["to_text"]),
                distance=float(row["distance"]),
                shared_topic=str(row["shared_topic"]),
            )
            for row in rows
        ]

    def record_links(
        self,
        judgements: Sequence[Judgement],
        *,
        created_by: str,
        unrelated: Sequence[Pair] = (),
    ) -> dict[str, int]:
        """Write one batch: the links, and the pairs the judge left alone.

        The negative is written too. Without it the same pair is offered on every
        run, and the judge is not deterministic - so a base that re-judges every
        unlinked pair drifts toward "everything is related", one run at a time,
        and the drift looks exactly like the base learning more.

        No signature on either: decision 4 leaves linking to the machine.
        """
        written = 0
        left_alone = 0
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for judgement in judgements:
                    cursor.execute(
                        """
                        INSERT INTO kx.knowledge_links
                            (from_kind, from_id, to_kind, to_id, link_type, created_by, method)
                        VALUES ('claim', %s, 'claim', %s, %s, %s, 'model')
                        ON CONFLICT DO NOTHING
                        """,
                        (judgement.from_id, judgement.to_id, judgement.link_type, created_by),
                    )
                    written += cursor.rowcount
                for pair in unrelated:
                    cursor.execute(
                        """
                        INSERT INTO kx.link_judgements (from_id, to_id, judged_by)
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (pair.from_id, pair.to_id, created_by),
                    )
                    left_alone += cursor.rowcount
        return {"written": written, "leftAlone": left_alone}

    def decide_wiki_suggestion(self, *, item_id: str, verdict: str, actor: str) -> dict[str, Any]:
        """Her verdict on "this statement belongs on that page".

        The row is written at decision time rather than proposed in advance:
        thousands of unreviewed bindings in `concept_evidence` is what the
        retired queue already showed to be unworkable. A rejection is a row too -
        without one, a page she looked at and said no to looks exactly like one
        nobody opened, and the queue offers it again tomorrow.
        """
        if verdict not in ("confirmed", "rejected"):
            raise ValueError("verdict must be 'confirmed' or 'rejected'")
        concept_claim_id, _, claim_id = item_id.partition("/")
        if not concept_claim_id or not claim_id:
            raise ValueError("item id must be 'concept_claim_id/claim_id'")
        # Each branch clears the other side. `concept_evidence` carries CHECK
        # (confirmed_at IS NULL OR rejected_at IS NULL), so stamping one column on
        # a row that already holds the other raised a constraint violation - which
        # meant changing a verdict was the one thing this could not do.
        stamped = (
            "confirmed_at = clock_timestamp(), confirmed_by = %(actor)s,"
            " rejected_at = NULL, rejected_by = NULL"
            if verdict == "confirmed"
            else "rejected_at = clock_timestamp(), rejected_by = %(actor)s,"
            " confirmed_at = NULL, confirmed_by = NULL"
        )
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO kx.concept_evidence (
                        concept_claim_id, claim_id, membership_class, binding_method,
                        stance, created_by, {
                        "confirmed_at, confirmed_by"
                        if verdict == "confirmed"
                        else "rejected_at, rejected_by"
                    }
                    )
                    VALUES (%(concept_claim_id)s, %(claim_id)s, 'historical', 'manual',
                            'supports', %(actor)s, clock_timestamp(), %(actor)s)
                    ON CONFLICT (concept_claim_id, claim_id) DO UPDATE SET {stamped}
                    """,  # noqa: S608 - both branches are constants chosen above
                    {"concept_claim_id": concept_claim_id, "claim_id": claim_id, "actor": actor},
                )
        return {"conceptClaimId": concept_claim_id, "claimId": claim_id, "verdict": verdict}

    def linking_report(self) -> dict[str, Any]:
        """What the base is linked by, and how much of it is reachable at all."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT link_type, count(*) AS total FROM kx.knowledge_links"
                " WHERE from_kind = 'claim' GROUP BY 1 ORDER BY 2 DESC"
            )
            by_type = [dict(row) for row in cursor.fetchall()]
            cursor.execute("SELECT count(*) AS looked_at_and_left_alone FROM kx.link_judgements")
            left_alone = dict(one_row(cursor))
            cursor.execute(
                """
                SELECT count(DISTINCT claim_id) AS linked_statements
                FROM (
                    SELECT from_id AS claim_id FROM kx.knowledge_links WHERE from_kind = 'claim'
                    UNION
                    SELECT to_id FROM kx.knowledge_links WHERE to_kind = 'claim'
                ) AS touched
                """
            )
            reach = dict(one_row(cursor))
        return {"byLinkType": by_type, **reach, **left_alone}

    # ---------------------------------------------------------------------
    # Stage 3: what the agent mode reads. Only `agent.*` - see migration 024.
    # ---------------------------------------------------------------------

    def agent_topics(self) -> list[dict[str, Any]]:
        """The backbone, with how much the base holds under each subject."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT topic_key, title, level, path, statements FROM agent.topic ORDER BY path"
            )
            return [dict(row) for row in cursor.fetchall()]

    def agent_concept(self, topic_key: str) -> dict[str, Any] | None:
        """One card: the subject, what stands under it, and how strong that is.

        Built from `claim_topics` rather than from the graph, because the graph
        holds provenance and has neither subjects nor entities in it (measured
        2026-08-23). The card is the statements, their labels and their spread.
        """
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT topic_key, title, level, path, statements FROM agent.topic"
                " WHERE topic_key = %s",
                (topic_key,),
            )
            topic = cursor.fetchone()
            if topic is None:
                return None
            cursor.execute(
                """
                SELECT statement.claim_id, statement.statement, statement.quote_text,
                       statement.source_url, statement.source_title,
                       statement.material_kind, statement.admission, statement.status,
                       statement.primary_source, statement.is_retelling,
                       statement.shown_on, statement.shown_kind
                FROM agent.statement AS statement
                JOIN agent.statement_topic AS placed USING (claim_id)
                WHERE placed.topic_key = %s AND statement.admission = 'knowledge'
                ORDER BY statement.shown_on DESC NULLS LAST, statement.claim_id
                LIMIT 60
                """,
                (topic_key,),
            )
            statements = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT statement.material_kind, statement.status, count(*) AS total
                FROM agent.statement AS statement
                JOIN agent.statement_topic AS placed USING (claim_id)
                WHERE placed.topic_key = %s
                GROUP BY 1, 2 ORDER BY 3 DESC
                """,
                (topic_key,),
            )
            spread = [dict(row) for row in cursor.fetchall()]
        return {**dict(topic), "statements": statements, "spread": spread}

    def agent_observatory(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        kind: str | None = None,
        fresh: bool = False,
    ) -> list[dict[str, Any]]:
        """A cut by class of event over a period (decision 4), not a feed.

        The cap is per class. A single cap over the whole cut, after ordering by
        class name, handed the alphabetically first class the entire budget - so
        a "cut by class of event" returned exactly one class and looked, from the
        outside, like a base that had only ever seen кейсы.

        Sorted newest first inside each class, and every row says which date it is
        showing - the source's own, or the day the radar found it. A chronicle
        that mixes the two silently dates an article to the day somebody crawled
        it.
        """
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT claim_id, statement, quote_text, char_start, char_end,
                       source_url, source_title, material_kind, primary_source,
                       is_retelling, shown_on, shown_kind, status, valid_until
                FROM (
                    SELECT statement.*,
                           row_number() OVER (
                               PARTITION BY statement.material_kind
                               ORDER BY statement.shown_on DESC NULLS LAST,
                                        statement.claim_id
                           ) AS within_class
                    FROM agent.statement AS statement
                    WHERE statement.admission = 'observatory'
                      AND (%s::date IS NULL OR statement.shown_on >= %s::date)
                      AND (%s::date IS NULL OR statement.shown_on <= %s::date)
                      AND (%s::text IS NULL OR statement.material_kind = %s::text)
                      AND (
                          NOT %s::boolean
                          OR statement.valid_until IS NULL
                          OR statement.valid_until >= clock_timestamp()
                      )
                ) AS by_class
                -- The cap is per class, not over the whole cut. A single LIMIT
                -- after an alphabetical sort by kind gave the first class the
                -- entire budget: "срез по классам событий" showed one class and
                -- the reader had no way to know the others existed.
                WHERE within_class <= %s
                ORDER BY material_kind, shown_on DESC NULLS LAST, claim_id
                """,
                (since, since, until, until, kind, kind, fresh, OBSERVATORY_PER_CLASS),
            )
            return [dict(row) for row in cursor.fetchall()]

    def agent_contradictions(self, *, limit: int = 60) -> tuple[int, list[dict[str, Any]]]:
        """Every pair the judge called incompatible, both sides shown together.

        283 of the 15 414 links are `contradicts`, and they are the reason a base
        like this is worth reading: two sources saying incompatible things about
        one subject is the finding, not a defect to hide. A card that shows only
        the statement you arrived at cannot show that - by the time a reader has
        found one side, they have no way to know the other exists.

        Symmetric, so the pair is rendered once: which claim sits in `from_id` is
        decided by comparing two uuids and carries nothing (see
        `promotion_candidates`). The stable order is the newer side first, so a
        reader meets the disagreement at its most recent end.
        """
        query = """
            SELECT link.from_id, link.to_id,
                   first.statement AS first_statement, first.quote_text AS first_quote,
                   first.source_url AS first_source_url,
                   first.source_title AS first_source_title,
                   first.material_kind AS first_material_kind,
                   first.status AS first_status, first.shown_on AS first_shown_on,
                   first.shown_kind AS first_shown_kind,
                   first.primary_source AS first_primary_source,
                   first.is_retelling AS first_is_retelling,
                   first.char_start AS first_char_start, first.char_end AS first_char_end,
                   first.valid_until AS first_valid_until,
                   second.statement AS second_statement, second.quote_text AS second_quote,
                   second.source_url AS second_source_url,
                   second.source_title AS second_source_title,
                   second.material_kind AS second_material_kind,
                   second.status AS second_status, second.shown_on AS second_shown_on,
                   second.shown_kind AS second_shown_kind,
                   second.primary_source AS second_primary_source,
                   second.is_retelling AS second_is_retelling,
                   second.char_start AS second_char_start, second.char_end AS second_char_end,
                   second.valid_until AS second_valid_until,
                   (
                       SELECT array_agg(DISTINCT placed.title ORDER BY placed.title)
                       FROM agent.statement_topic AS placed
                       WHERE placed.claim_id IN (link.from_id, link.to_id)
                   ) AS topics
            FROM agent.link AS link
            JOIN agent.statement AS first ON first.claim_id = link.from_id
            JOIN agent.statement AS second ON second.claim_id = link.to_id
            WHERE link.link_type = 'contradicts'
        """
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) AS total FROM ({query}) AS all_of_them")  # noqa: S608
            total = int(one_row(cursor)["total"])
            ordered = (
                f"{query} ORDER BY greatest(first.shown_on, second.shown_on)"
                " DESC NULLS LAST, link.from_id LIMIT %s"
            )
            cursor.execute(ordered, (limit,))
            return total, [dict(row) for row in cursor.fetchall()]

    def agent_gaps(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT claim_id, missing, statement FROM agent.gap ORDER BY claim_id LIMIT %s",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def agent_pages(self) -> list[dict[str, Any]]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT relative_path, title, language, length(body) AS chars"
                " FROM agent.page ORDER BY relative_path"
            )
            return [dict(row) for row in cursor.fetchall()]

    def agent_page(self, relative_path: str) -> dict[str, Any] | None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT relative_path, title, body, language FROM agent.page"
                " WHERE relative_path = %s",
                (relative_path,),
            )
            row = cursor.fetchone()
            return None if row is None else dict(row)

    def agent_search(
        self,
        question: str,
        *,
        filters: Mapping[str, str | None] | None = None,
        limit: int = 10,
        question_vector: str | None = None,
    ) -> list[dict[str, Any]]:
        """The reader's own search: hybrid, filtered, and saying why each hit is here."""
        unknown = set(filters or ()) - set(AGENT_FILTERS)
        if unknown:
            raise ValueError(f"unknown filters {sorted(unknown)}; expected {list(AGENT_FILTERS)}")
        parameters: dict[str, Any] = {
            "question": question,
            "rrf_k": RRF_K,
            "limit": limit,
            "question_vector": question_vector,
            "embedding_model": DEFAULT_MODEL,
            "semantic_depth": SEMANTIC_DEPTH,
            **{name: (filters or {}).get(name) for name in AGENT_FILTERS},
        }
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(AGENT_SEARCH_SQL, parameters)
            return [dict(row) for row in cursor.fetchall()]

    def agent_statement(self, claim_id: str) -> dict[str, Any] | None:
        """One statement at all four levels, and what it has been linked to."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT claim_id, statement, quote_text, char_start, char_end,
                       source_url, source_title, language, material_kind, admission,
                       primary_source, is_retelling, valid_until,
                       published_on, shown_on, shown_kind, status, status_method
                FROM agent.statement WHERE claim_id = %s
                """,
                (claim_id,),
            )
            found = cursor.fetchone()
            if found is None:
                return None
            cursor.execute(
                "SELECT topic_key, title, level FROM agent.statement_topic"
                " WHERE claim_id = %s ORDER BY level, title",
                (claim_id,),
            )
            topics = [dict(row) for row in cursor.fetchall()]
            # `agent.link` stores each pair once, in uuid order, so half of every
            # statement's links point at it rather than out of it. Reading only
            # `from_id` hid those: 794 statements on production have links and
            # would have rendered none at all.
            #
            # `direction` comes back because it is the only thing that makes
            # `qualifies` readable. The judge answers about an ordered pair - "the
            # second qualifies the first" - so on an outgoing link the *other*
            # statement qualifies this one, and on an incoming link this one
            # qualifies the other. `supports`, `contradicts` and `related_to` say
            # the same thing whichever end you read them from.
            cursor.execute(
                """
                WITH either_way AS (
                    SELECT link.link_type, link.to_id AS claim_id, 'outgoing' AS direction
                    FROM agent.link AS link
                    WHERE link.from_id = %(claim_id)s
                    UNION ALL
                    SELECT link.link_type, link.from_id AS claim_id, 'incoming' AS direction
                    FROM agent.link AS link
                    WHERE link.to_id = %(claim_id)s
                )
                SELECT either_way.link_type, either_way.direction, either_way.claim_id,
                       other.statement, other.material_kind, other.status
                FROM either_way
                JOIN agent.statement AS other USING (claim_id)
                ORDER BY either_way.link_type, other.claim_id
                LIMIT 40
                """,
                {"claim_id": claim_id},
            )
            links = [dict(row) for row in cursor.fetchall()]
        return {**dict(found), "topics": topics, "links": links}

    # ---------------------------------------------------------------------
    # Stage 0b: reading a statement (owner decisions 1, 3, 7, 8, 11)
    # ---------------------------------------------------------------------

    def unread_claims(self, *, limit: int = 100000) -> list[ReadableClaim]:
        """Statements nobody has read yet, with what the reader needs to read them.

        The quotation comes along because half the decisions cannot be made from
        the normalised claim alone: whether a sentence is a forecast or a fact is
        in its words, and the normalised form has dropped most of them.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT claims.claim_id,
                           claims.normalized_text,
                           evidence.quote_text,
                           CASE WHEN documents.canonical_url LIKE 'agpm-canon:%%'
                                THEN 'канон AgPM' ELSE 'материал выпуска' END AS corpus,
                           coalesce(dates.published_on, dates.shown_on) AS dated_on
                    FROM kx.claims AS claims
                    JOIN kx.claim_evidence AS evidence
                      ON evidence.claim_id = claims.claim_id
                     AND evidence.match_status = 'exact'
                    JOIN kx.document_versions AS versions
                      ON versions.version_id = claims.version_id
                    JOIN kx.documents AS documents
                      ON documents.document_id = versions.document_id
                    LEFT JOIN kx.document_dates AS dates
                           ON dates.document_id = documents.document_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM kx.claim_reading AS reading
                        WHERE reading.claim_id = claims.claim_id
                    )
                    ORDER BY claims.claim_id
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
        return [
            ReadableClaim(
                claim_id=str(row["claim_id"]),
                statement=str(row["normalized_text"]),
                quote=str(row["quote_text"]),
                corpus=str(row["corpus"]),
                dated_on=(
                    datetime.combine(row["dated_on"], time.min, tzinfo=UTC)
                    if row["dated_on"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def freshness_rules(self) -> dict[str, timedelta | None]:
        """The owner's interval per kind of material, as migration 022 recorded it."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT material_kind, valid_for FROM kx.material_kind_freshness")
            return {str(row["material_kind"]): row["valid_for"] for row in cursor.fetchall()}

    def record_readings(
        self,
        readings: Sequence[Reading],
        claims: Mapping[str, ReadableClaim],
        freshness: Mapping[str, timedelta | None],
        *,
        read_by: str,
    ) -> dict[str, int]:
        """Write one batch: the reading, its subjects, and the gap it leaves if any.

        One transaction per batch, so a call whose answer was only half usable
        leaves either all of its rows or none. A subject key the backbone does not
        have writes nothing: the `SELECT ... FROM topics` is the check.
        """
        written = {"readings": 0, "topics": 0, "gaps": 0}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for reading in readings:
                    claim = claims[reading.claim_id]
                    cursor.execute(
                        """
                        INSERT INTO kx.claim_reading (
                            claim_id, material_kind, primary_source, is_retelling,
                            admission, admission_note, valid_until, confidence,
                            read_by, method
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'model')
                        ON CONFLICT (claim_id) DO NOTHING
                        """,
                        (
                            reading.claim_id,
                            reading.material_kind,
                            reading.primary_source,
                            reading.is_retelling,
                            reading.admission,
                            reading.admission_note,
                            expiry_of(reading.material_kind, claim.dated_on, freshness),
                            reading.confidence,
                            read_by,
                        ),
                    )
                    written["readings"] += cursor.rowcount
                    # Which of the named subjects the backbone actually has. The
                    # insert below used to be the check, and it could not be one:
                    # a key the backbone does not carry inserts no row and says
                    # nothing, so a statement whose every subject was invented
                    # left neither a placement nor a gap. Six statements on
                    # production sit in that hole.
                    accepted: list[str] = []
                    if reading.topic_keys:
                        cursor.execute(
                            "SELECT topic_key FROM kx.topics"
                            " WHERE topic_key = ANY(%s::text[]) AND state = 'accepted'",
                            (list(reading.topic_keys),),
                        )
                        accepted = [str(row["topic_key"]) for row in cursor.fetchall()]
                    for topic_key in accepted:
                        cursor.execute(
                            """
                            INSERT INTO kx.claim_topics
                                (claim_id, topic_id, confidence, assigned_by, method)
                            SELECT %s, topics.topic_id, %s, %s, 'model'
                            FROM kx.topics AS topics
                            WHERE topics.topic_key = %s AND topics.state = 'accepted'
                            ON CONFLICT DO NOTHING
                            """,
                            (reading.claim_id, reading.confidence, read_by, topic_key),
                        )
                        written["topics"] += cursor.rowcount
                    # Decision 8's queue is about what the *base* cannot place. A
                    # statement the reading threw out is not in the base, so what
                    # it lacks is not a gap in it - 2 300 of the 2 628 rows this
                    # wrote on production were rejected statements.
                    if not accepted and reading.admission != "rejected":
                        cursor.execute(
                            """
                            INSERT INTO kx.claim_gaps (claim_id, missing, noted_by, method)
                            VALUES (%s, %s, %s, 'model')
                            ON CONFLICT (claim_id) DO NOTHING
                            """,
                            (
                                reading.claim_id,
                                reading.missing or not_in_the_backbone(reading.topic_keys),
                                read_by,
                            ),
                        )
                        written["gaps"] += cursor.rowcount
        return written

    def grant_birth_statuses(self, *, set_by: str) -> dict[str, int]:
        """Give every claim without one the status its corpus is born with.

        A rule, not a judgement, which is why `method` says so: a statement out of
        the AgPM canon is canon, one out of somebody else's standard is an external
        reference, and one out of a radar issue is an observed signal until the
        owner promotes it (§2.2, decision 6). Append-only, so re-running adds
        nothing to a claim that already carries one.
        """
        # Imported here rather than at the top: canon_corpus imports this module,
        # and the authorship list belongs beside the fidelity table it sits with.
        from radar_kx.canon_corpus import AGPM_AUTHORED

        authored = [f"agpm-canon:/{stem}%" for stem in sorted(AGPM_AUTHORED)]
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO kx.knowledge_status
                        (unit_kind, unit_id, status, method, rationale, set_by)
                    SELECT 'claim', claims.claim_id,
                           CASE
                               WHEN documents.canonical_url NOT LIKE 'agpm-canon:%%'
                                   THEN 'observed_signal'
                               WHEN documents.canonical_url LIKE ANY (%s)
                                   THEN 'canon'
                               ELSE 'external_reference'
                           END,
                           'rule',
                           'статус при рождении, по корпусу документа',
                           %s
                    FROM kx.claims AS claims
                    JOIN kx.document_versions AS versions
                      ON versions.version_id = claims.version_id
                    JOIN kx.documents AS documents
                      ON documents.document_id = versions.document_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM kx.knowledge_status AS granted
                        WHERE granted.unit_kind = 'claim'
                          AND granted.unit_id = claims.claim_id
                    )
                    """,
                    (authored, set_by),
                )
                return {"granted": cursor.rowcount}

    def reading_report(self) -> dict[str, Any]:
        """How much has been read, and what the reading found."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT (SELECT count(*) FROM kx.claims) AS claims,
                       (SELECT count(*) FROM kx.claim_reading) AS have_been_read,
                       (SELECT count(DISTINCT claim_id) FROM kx.claim_topics)
                           AS with_a_subject,
                       (SELECT count(*) FROM kx.claim_gaps) AS gaps,
                       (SELECT count(*) FROM kx.knowledge_status WHERE unit_kind = 'claim')
                           AS statuses
                """
            )
            totals = dict(one_row(cursor))
            cursor.execute(
                "SELECT admission, material_kind, count(*) AS total FROM kx.claim_reading"
                " GROUP BY 1, 2 ORDER BY 1, 3 DESC"
            )
            spread = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT primary_source, count(*) AS total FROM kx.claim_reading"
                " WHERE is_retelling GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
            )
            retold = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT status, count(*) AS total FROM kx.knowledge_status_current"
                " WHERE unit_kind = 'claim' GROUP BY 1 ORDER BY 2 DESC"
            )
            statuses = [dict(row) for row in cursor.fetchall()]
        return {
            **totals,
            "byAdmissionAndKind": spread,
            "mostRetoldSources": retold,
            "byStatus": statuses,
        }

    # ---------------------------------------------------------------------
    # Stage 0a: putting quotation boundaries back on their sentences
    # ---------------------------------------------------------------------

    def plan_span_repair(self) -> list[Repair]:
        """Read every exact span and work out where it should sit.

        The canonical texts are read once and kept - 311 versions, seven megabytes
        - because 13 876 spans point into them, and joining the text onto every row
        would move the same documents across the wire forty-five times over.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT version_id, canonical_text FROM kx.document_versions
                    WHERE version_id IN (
                        SELECT DISTINCT version_id FROM kx.claim_evidence
                        WHERE match_status = 'exact'
                    )
                    """
                )
                texts = {row["version_id"]: row["canonical_text"] for row in cursor.fetchall()}
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT claim_id, version_id, char_start, char_end, quote_text"
                    " FROM kx.claim_evidence WHERE match_status = 'exact'"
                    " ORDER BY version_id, char_start"
                )
                rows = cursor.fetchall()
        repairs = []
        for row in rows:
            text = texts[row["version_id"]]
            expansion = expand_span(text, row["char_start"], row["char_end"])
            repairs.append(
                Repair(
                    claim_id=str(row["claim_id"]),
                    version_id=str(row["version_id"]),
                    old_start=row["char_start"],
                    old_end=row["char_end"],
                    quote=str(row["quote_text"]),
                    widened=text[expansion.start : expansion.end],
                    expansion=expansion,
                )
            )
        return repairs

    def apply_span_repair(self, repairs: Sequence[Repair]) -> dict[str, Any]:
        """Widen the spans that moved, in place, one transaction for all of them.

        The primary key of `claim_evidence` carries the coordinates, so this is an
        update of the key itself. That is safe here and would not be elsewhere: one
        claim has exactly one span, so no widening can land on another row. The
        trigger re-derives the quotation from the canonical text on each of these
        writes, which is what makes the repair checkable rather than trusted.

        The vectors of the widened quotations are left stale on purpose - a text
        update does not write a vector - and `embed` picks them up on its next pass
        because their stored fingerprint stops matching the text they name.
        """
        moved = [repair for repair in repairs if repair.changed]
        if not moved:
            return {"updated": 0}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for repair in moved:
                    quote = self._widened_quote(cursor, repair)
                    if quote != repair.widened:
                        raise RuntimeError(
                            f"the canonical text behind claim {repair.claim_id} changed"
                            " between the plan and the write; re-run the dry run"
                        )
                    cursor.execute(
                        """
                        UPDATE kx.claim_evidence
                           SET char_start = %s, char_end = %s,
                               quote_text = %s, quote_sha256 = %s
                         WHERE claim_id = %s AND version_id = %s
                           AND char_start = %s AND char_end = %s
                        """,
                        (
                            repair.expansion.start,
                            repair.expansion.end,
                            quote,
                            sha256_bytes(quote.encode("utf-8")),
                            repair.claim_id,
                            repair.version_id,
                            repair.old_start,
                            repair.old_end,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"the span repair of claim {repair.claim_id} matched"
                            f" {cursor.rowcount} rows rather than one"
                        )
        return {"updated": len(moved)}

    @staticmethod
    def _widened_quote(cursor: psycopg.Cursor[dict[str, Any]], repair: Repair) -> str:
        """The widened quotation, cut out of the store rather than assembled here.

        The plan read the canonical text minutes or hours before the write. Cutting
        the span again at write time means the text that is stored is the text the
        database itself produced, and the trigger then has something real to check.
        """
        cursor.execute(
            "SELECT substr(canonical_text, %s, %s) AS quote FROM kx.document_versions"
            " WHERE version_id = %s",
            (
                repair.expansion.start + 1,
                repair.expansion.end - repair.expansion.start,
                repair.version_id,
            ),
        )
        return str(one_row(cursor)["quote"])

    def plan_document_dates(self) -> list[ResolvedDate]:
        """Read what every document says about its own publication date.

        The issue perimeter and the corpus row are read side by side rather than
        coalesced in SQL, because which of the two answered has to end up in the
        row: a date nobody can trace to the text it came from cannot be corrected
        when it turns out to be wrong.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT documents.document_id,
                           (SELECT max(members.published_raw)
                              FROM kx.issue_perimeter_members AS members
                             WHERE members.document_id = documents.document_id) AS issue_raw,
                           (SELECT max(materials.published_raw)
                              FROM kx.material_documents AS links
                              JOIN kx.source_materials AS materials USING (material_id)
                             WHERE links.document_id = documents.document_id) AS material_raw,
                           coalesce(
                               documents.first_seen_at,
                               (SELECT min(versions.fetched_at)
                                  FROM kx.document_versions AS versions
                                 WHERE versions.document_id = documents.document_id)
                           ) AS found_at
                    FROM kx.documents AS documents
                    ORDER BY documents.document_id
                    """
                )
                rows = cursor.fetchall()
        return [
            resolve_date(
                document_id=str(row["document_id"]),
                issue_raw=row["issue_raw"],
                material_raw=row["material_raw"],
                found_at=row["found_at"],
            )
            for row in rows
            if row["found_at"] is not None
        ]

    def apply_document_dates(self, dates: Sequence[ResolvedDate]) -> dict[str, Any]:
        """Store one row per document, replacing whatever a previous pass left.

        A recomputation, not a decision: re-running with a better parser has to
        leave one row per document rather than a second opinion beside the first.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                for resolved in dates:
                    cursor.execute(
                        """
                        INSERT INTO kx.document_dates (
                            document_id, published_raw, raw_source,
                            published_on, date_precision, shown_on, shown_kind
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (document_id) DO UPDATE SET
                            published_raw = EXCLUDED.published_raw,
                            raw_source = EXCLUDED.raw_source,
                            published_on = EXCLUDED.published_on,
                            date_precision = EXCLUDED.date_precision,
                            shown_on = EXCLUDED.shown_on,
                            shown_kind = EXCLUDED.shown_kind,
                            resolved_at = clock_timestamp()
                        """,
                        (
                            resolved.document_id,
                            resolved.published_raw,
                            resolved.raw_source,
                            resolved.published_on,
                            resolved.date_precision,
                            resolved.shown_on,
                            resolved.shown_kind,
                        ),
                    )
        return {"stored": len(dates)}

    # ---------------------------------------------------------------------
    # Embeddings and the comparison they exist for (owner request, 2026-08-23)
    # ---------------------------------------------------------------------

    #: What each owner kind reads, and whether it is the asking side. e5 wants
    #: `query:` on the side asking and `passage:` on the side searched.
    _EMBED_SOURCES: ClassVar[dict[str, tuple[str, bool]]] = {
        "concept_claim": (
            "SELECT concept_claim_id AS key, statement AS text FROM kx.concept_claims",
            True,
        ),
        "claim_evidence": (
            "SELECT claim_id AS key, quote_text AS text FROM kx.claim_evidence"
            " WHERE match_status = 'exact'",
            False,
        ),
        # Stage 1: the corpus itself, not only the 5% of it that was extracted
        # from. A chunk is capped at 4 000 characters by the store and e5 reads
        # about 2 000 of them, so a long chunk is embedded by its first half -
        # which is the model's context window rather than a choice made here.
        "chunk": ("SELECT chunk_id AS key, text FROM kx.chunks", False),
    }

    def embed(
        self, owner_kind: str, *, model_id: str = DEFAULT_MODEL, limit: int = 100000
    ) -> dict[str, Any]:
        """Encode everything of one kind whose stored vector is missing or stale.

        Stale is checked, not assumed: `text_embeddings` records the hash of what
        was encoded, so a row whose text has since changed is recognisable without
        anybody having to remember to say so. Stage 0a widens 4 800 quotations, and
        a vector still describing the torn version of one of them would quietly
        answer searches with text that is no longer there.
        """
        if owner_kind not in self._EMBED_SOURCES:
            raise ValueError(f"owner_kind must be one of {sorted(self._EMBED_SOURCES)}")
        query, is_query = self._EMBED_SOURCES[owner_kind]
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO kx.embedding_models (model_id, dimensions, provider, parameters)"
                    " VALUES (%s, %s, 'local', %s) ON CONFLICT (model_id) DO NOTHING",
                    (
                        model_id,
                        DEFAULT_DIMENSIONS,
                        Jsonb({"runtime": "radar-embed-runtime", "device": "cpu"}),
                    ),
                )
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT source.key::text AS key, source.text
                    FROM ({query}) AS source
                    LEFT JOIN kx.text_embeddings AS stored
                           ON stored.owner_kind = %s
                          AND stored.owner_key = source.key::text
                          AND stored.model_id = %s
                    WHERE stored.owner_key IS NULL
                       OR stored.text_sha256 <> encode(
                              digest(convert_to(source.text, 'UTF8'), 'sha256'), 'hex'
                          )
                    LIMIT %s
                    """,  # noqa: S608 - a constant from _EMBED_SOURCES
                    (owner_kind, model_id, limit),
                )
                rows = [dict(row) for row in cursor.fetchall()]
        if not rows:
            return {"ownerKind": owner_kind, "encoded": 0, "modelId": model_id}

        model = load_model(model_id)
        written = 0
        with self.connect() as connection:
            for start in range(0, len(rows), 500):
                block = rows[start : start + 500]
                vectors = encode(model, [str(row["text"]) for row in block], is_query=is_query)
                with connection.transaction(), connection.cursor() as cursor:
                    for row, vector in zip(block, vectors, strict=True):
                        cursor.execute(
                            "INSERT INTO kx.text_embeddings"
                            " (owner_kind, owner_key, model_id, text_sha256, embedding)"
                            " VALUES (%s, %s, %s, %s, %s::vector)"
                            " ON CONFLICT (owner_kind, owner_key, model_id) DO UPDATE"
                            " SET text_sha256 = EXCLUDED.text_sha256,"
                            "     embedding = EXCLUDED.embedding,"
                            "     created_at = clock_timestamp()"
                            " WHERE text_embeddings.text_sha256 <> EXCLUDED.text_sha256",
                            (
                                owner_kind,
                                str(row["key"]),
                                model_id,
                                text_fingerprint(str(row["text"])),
                                to_pgvector(vector),
                            ),
                        )
                        written += cursor.rowcount
        return {"ownerKind": owner_kind, "encoded": written, "modelId": model_id}

    def compare_binding_methods(
        self, *, model_id: str = DEFAULT_MODEL, top: int = 5, ran_by: str = "radar-kx"
    ) -> dict[str, Any]:
        """Run both linking methods over the same statements and record both.

        Lexical is the reciprocal-rank fusion of slice 2.5; semantic is cosine
        distance over locally computed embeddings. Nothing here decides which is
        better - it puts the two answers for every statement side by side, which
        is what the owner asked for and what an argument about quality needs.
        """
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT claims.concept_claim_id, claims.statement"
                    " FROM kx.concept_claims AS claims"
                    " WHERE claims.claim_nature <> 'open_question'"
                    "   AND EXISTS (SELECT 1 FROM kx.text_embeddings AS vectors"
                    "               WHERE vectors.owner_kind = 'concept_claim'"
                    "                 AND vectors.owner_key = claims.concept_claim_id::text"
                    "                 AND vectors.model_id = %s)"
                    " ORDER BY claims.concept_claim_id",
                    (model_id,),
                )
                statements = [dict(row) for row in cursor.fetchall()]

            detail: list[dict[str, Any]] = []
            agree_top = 0
            overlap_total = 0
            semantic_only = 0
            for row in statements:
                statement_id = str(row["concept_claim_id"])
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT stored.owner_key AS claim_id,
                               1 - (asked.embedding <=> stored.embedding) AS score,
                               evidence.quote_text
                        FROM kx.text_embeddings AS asked
                        JOIN kx.text_embeddings AS stored
                          ON stored.owner_kind = 'claim_evidence'
                         AND stored.model_id = asked.model_id
                        JOIN kx.claim_evidence AS evidence
                          ON evidence.claim_id::text = stored.owner_key
                        WHERE asked.owner_kind = 'concept_claim'
                          AND asked.owner_key = %s
                          AND asked.model_id = %s
                        ORDER BY asked.embedding <=> stored.embedding
                        LIMIT %s
                        """,
                        (statement_id, model_id, top),
                    )
                    semantic = [dict(item) for item in cursor.fetchall()]
                    cursor.execute(
                        "SELECT claim_id::text AS claim_id, relevance"
                        " FROM kx.concept_evidence WHERE concept_claim_id = %s"
                        " ORDER BY relevance DESC LIMIT %s",
                        (statement_id, top),
                    )
                    lexical = [dict(item) for item in cursor.fetchall()]

                semantic_ids = [str(item["claim_id"]) for item in semantic]
                lexical_ids = [str(item["claim_id"]) for item in lexical]
                shared = set(semantic_ids) & set(lexical_ids)
                overlap_total += len(shared)
                if semantic_ids and lexical_ids and semantic_ids[0] == lexical_ids[0]:
                    agree_top += 1
                if semantic_ids and not shared:
                    semantic_only += 1
                detail.append(
                    {
                        "conceptClaimId": statement_id,
                        "statement": str(row["statement"])[:300],
                        "semanticTop": (
                            {
                                "claimId": semantic_ids[0],
                                "score": round(float(semantic[0]["score"]), 4),
                                "quote": str(semantic[0]["quote_text"])[:300],
                            }
                            if semantic
                            else None
                        ),
                        "lexicalTop": ({"claimId": lexical_ids[0]} if lexical_ids else None),
                        "overlapAtTop": len(shared),
                    }
                )

            summary = {
                "model": model_id,
                "top": top,
                "statements": len(statements),
                "sameFirstChoice": agree_top,
                "averageOverlapAtTop": (
                    round(overlap_total / len(statements), 3) if statements else 0
                ),
                "statementsWhereMethodsShareNothing": semantic_only,
            }
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO kx.binding_method_comparisons"
                    " (ran_by, statements, summary, detail) VALUES (%s, %s, %s, %s)"
                    " RETURNING comparison_id",
                    (ran_by, len(statements), Jsonb(summary), Jsonb(detail[:400])),
                )
                comparison_id = str(one_row(cursor)["comparison_id"])
        return {"comparisonId": comparison_id, **summary, "examples": detail[:5]}

    #: Everything a binding could reach: every document, restricted by nothing. The
    #: first comparison used the stored proposals for the lexical side, which came
    #: from a different scope and floor than the semantic side ever saw. Running
    #: both live against the same population is what makes the four numbers
    #: comparable at all.
    _ANY_DOCUMENT_SQL: ClassVar[str] = "SELECT document_id FROM kx.documents"

    #: Only the documents that sit under the same level-1 section as the statement.
    _TOPIC_DOCUMENT_SQL: ClassVar[str] = (
        "SELECT DISTINCT document_id FROM kx.document_topics"
        " WHERE topic_id = ANY(%(topics)s::uuid[])"
    )

    _SEMANTIC_SQL: ClassVar[str] = """
        SELECT DISTINCT ON (stored.owner_key)
               stored.owner_key AS claim_id,
               1 - (asked.embedding <=> stored.embedding) AS score,
               evidence.quote_text
        FROM kx.text_embeddings AS asked
        JOIN kx.text_embeddings AS stored
          ON stored.owner_kind = 'claim_evidence'
         AND stored.model_id = asked.model_id
        JOIN kx.claim_evidence AS evidence
          ON evidence.claim_id::text = stored.owner_key
        JOIN kx.document_versions AS versions USING (version_id)
        WHERE asked.owner_kind = 'concept_claim'
          AND asked.owner_key = %(statement_id)s
          AND asked.model_id = %(model_id)s
          {restriction}
        ORDER BY stored.owner_key, asked.embedding <=> stored.embedding
    """

    #: `DISTINCT ON` has to sort by its own key first, so the ranking is done once
    #: more on the outside. Without the distinct, one claim quoted twice takes two
    #: of the five places and the two methods are compared over different-sized
    #: shortlists.
    _SEMANTIC_RANKED_SQL: ClassVar[str] = (
        "SELECT claim_id, score, quote_text FROM ({inner}) AS best"
        " ORDER BY score DESC LIMIT %(limit)s"
    )

    _TOPIC_RESTRICTION: ClassVar[str] = (
        "AND EXISTS (SELECT 1 FROM kx.document_topics AS placed"
        "            WHERE placed.document_id = versions.document_id"
        "              AND placed.topic_id = ANY(%(topics)s::uuid[]))"
    )

    def _topic_subtrees(self) -> dict[str, list[str]]:
        """For every topic, the ids of everything under its level-1 section.

        Two things are on the same subject when they sit under the same category,
        not when they were tagged with the identical leaf. A statement about
        escalation and a quotation about decision rights are both about management
        mechanisms, and a comparison that called those different subjects would be
        measuring the tagger's word choice.
        """
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT topic_id::text AS topic_id, parent_id::text AS parent_id"
                " FROM kx.topics WHERE state = 'accepted'"
            )
            parents = {
                str(row["topic_id"]): (str(row["parent_id"]) if row["parent_id"] else None)
                for row in cursor.fetchall()
            }
        roots: dict[str, str] = {}
        for topic_id in parents:
            walk = topic_id
            while parents.get(walk):
                walk = cast(str, parents[walk])
            roots[topic_id] = walk
        members: dict[str, list[str]] = {}
        for topic_id, root in roots.items():
            members.setdefault(root, []).append(topic_id)
        return {topic_id: members[root] for topic_id, root in roots.items()}

    def _lexical_hits(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        *,
        statement: str,
        top: int,
        topics: list[str] | None,
    ) -> list[str]:
        sql = EVIDENCE_SEARCH_SQL.format(
            scope=self._TOPIC_DOCUMENT_SQL if topics is not None else self._ANY_DOCUMENT_SQL
        )
        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                {"statement": statement, "k": RRF_K, "limit": top, "topics": topics or []},
            )
            return [str(row["claim_id"]) for row in cursor.fetchall()]

    def _semantic_hits(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        *,
        statement_id: str,
        model_id: str,
        top: int,
        topics: list[str] | None,
    ) -> list[dict[str, Any]]:
        """Rows, not ids: the queue the owner votes in shows the quotation and the
        score beside it, and a comparison that recorded only ids would leave that
        queue with nothing to render."""
        inner = self._SEMANTIC_SQL.format(
            restriction=self._TOPIC_RESTRICTION if topics is not None else ""
        )
        with connection.cursor() as cursor:
            cursor.execute(
                self._SEMANTIC_RANKED_SQL.format(inner=inner),
                {
                    "statement_id": statement_id,
                    "model_id": model_id,
                    "limit": top,
                    "topics": topics or [],
                },
            )
            return [dict(row) for row in cursor.fetchall()]

    #: How many quotations one statement is offered. Five, because the question
    #: the owner asked is whether the right one sits below the first, and five is
    #: as deep as a person will read carefully.
    CANDIDATES_SHOWN: ClassVar[int] = 5

    @staticmethod
    def _merge_candidates(
        semantic: Sequence[str], lexical: Sequence[str], *, shown: int
    ) -> list[dict[str, Any]]:
        """Interleave both rankings into one shortlist, keeping where each came from.

        Interleaved rather than one method's five, because a shortlist drawn from a
        single method could only ever answer whether that method buries the right
        quotation. Alternating gives about three ranks of each and lets one choice
        answer both questions: which method found it, and how far down.
        """
        ranks: dict[str, dict[str, Any]] = {}
        for method, ordered in (("semantic", semantic), ("lexical", lexical)):
            for position, claim_id in enumerate(ordered, start=1):
                ranks.setdefault(claim_id, {"claimId": claim_id})[f"{method}Rank"] = position
        merged: list[dict[str, Any]] = []
        for position in range(max(len(semantic), len(lexical))):
            for ordered in (semantic, lexical):
                if position >= len(ordered):
                    continue
                entry = ranks[ordered[position]]
                if entry not in merged:
                    merged.append(entry)
                if len(merged) == shown:
                    return merged
        return merged

    def compare_binding_methods_within_topics(
        self, *, model_id: str = DEFAULT_MODEL, top: int = 5, ran_by: str = "radar-kx"
    ) -> dict[str, Any]:
        """Both methods, twice each: over everything, and inside the statement's subject.

        The first comparison could only measure agreement, because neither score
        means what it looks like and there is no gold set. The backbone adds a
        measurement that needs neither: **what share of what a method returns is
        even about the right subject**. That is answerable without deciding which
        of two plausible quotations is better, and it is the defect the owner named.
        """
        subtrees = self._topic_subtrees()
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT claims.concept_claim_id::text AS id, claims.statement,
                           array_agg(placed.topic_id::text) AS topic_ids
                    FROM kx.concept_claims AS claims
                    JOIN kx.concept_claim_topics AS placed USING (concept_claim_id)
                    WHERE claims.claim_nature <> 'open_question'
                      AND EXISTS (
                          SELECT 1 FROM kx.text_embeddings AS vectors
                          WHERE vectors.owner_kind = 'concept_claim'
                            AND vectors.owner_key = claims.concept_claim_id::text
                            AND vectors.model_id = %s
                      )
                    GROUP BY claims.concept_claim_id, claims.statement
                    ORDER BY claims.concept_claim_id
                    """,
                    (model_id,),
                )
                statements = [dict(row) for row in cursor.fetchall()]

            #: Which claims sit in which subject, so "did the answer land on the
            #: right subject" can be asked of the unrestricted runs too.
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT evidence.claim_id::text AS claim_id,
                           array_agg(DISTINCT placed.topic_id::text) AS topic_ids
                    FROM kx.claim_evidence AS evidence
                    JOIN kx.document_versions AS versions USING (version_id)
                    JOIN kx.document_topics AS placed USING (document_id)
                    WHERE evidence.match_status = 'exact'
                    GROUP BY evidence.claim_id
                    """
                )
                claim_topics = {
                    str(row["claim_id"]): {str(value) for value in row["topic_ids"]}
                    for row in cursor.fetchall()
                }

            runs = ("lexical", "semantic", "lexicalInTopic", "semanticInTopic")
            hit: dict[str, int] = dict.fromkeys(runs, 0)
            on_topic_first: dict[str, int] = dict.fromkeys(runs, 0)
            on_topic_all: dict[str, int] = dict.fromkeys(runs, 0)
            returned: dict[str, int] = dict.fromkeys(runs, 0)
            agree_open = 0
            agree_scoped = 0
            overlap_open = 0
            overlap_scoped = 0
            detail: list[dict[str, Any]] = []

            for row in statements:
                statement_id = str(row["id"])
                statement = str(row["statement"])
                scope_ids = sorted(
                    {
                        member
                        for topic_id in row["topic_ids"]
                        for member in subtrees.get(str(topic_id), [])
                    }
                )
                semantic = self._semantic_hits(
                    connection,
                    statement_id=statement_id,
                    model_id=model_id,
                    top=top,
                    topics=None,
                )
                semantic_in_topic = self._semantic_hits(
                    connection,
                    statement_id=statement_id,
                    model_id=model_id,
                    top=top,
                    topics=scope_ids,
                )
                answers = {
                    "lexical": self._lexical_hits(
                        connection, statement=statement, top=top, topics=None
                    ),
                    "semantic": [str(row["claim_id"]) for row in semantic],
                    "lexicalInTopic": self._lexical_hits(
                        connection, statement=statement, top=top, topics=scope_ids
                    ),
                    "semanticInTopic": [str(row["claim_id"]) for row in semantic_in_topic],
                }
                scope = set(scope_ids)
                for name, claims in answers.items():
                    if claims:
                        hit[name] += 1
                        if claim_topics.get(claims[0], set()) & scope:
                            on_topic_first[name] += 1
                    returned[name] += len(claims)
                    on_topic_all[name] += sum(
                        1 for claim in claims if claim_topics.get(claim, set()) & scope
                    )
                open_shared = set(answers["lexical"]) & set(answers["semantic"])
                scoped_shared = set(answers["lexicalInTopic"]) & set(answers["semanticInTopic"])
                overlap_open += len(open_shared)
                overlap_scoped += len(scoped_shared)
                if answers["lexical"][:1] and answers["lexical"][:1] == answers["semantic"][:1]:
                    agree_open += 1
                if (
                    answers["lexicalInTopic"][:1]
                    and answers["lexicalInTopic"][:1] == answers["semanticInTopic"][:1]
                ):
                    agree_scoped += 1
                candidates = self._merge_candidates(
                    answers["semanticInTopic"],
                    answers["lexicalInTopic"],
                    shown=self.CANDIDATES_SHOWN,
                )
                if candidates:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT DISTINCT ON (evidence.claim_id)"
                            " evidence.claim_id::text AS id, evidence.quote_text,"
                            " documents.canonical_url"
                            " FROM kx.claim_evidence AS evidence"
                            " JOIN kx.document_versions AS versions USING (version_id)"
                            " JOIN kx.documents AS documents USING (document_id)"
                            " WHERE evidence.claim_id::text = ANY(%s)"
                            "   AND evidence.match_status = 'exact'",
                            ([item["claimId"] for item in candidates],),
                        )
                        texts = {
                            str(item["id"]): (
                                str(item["quote_text"]),
                                str(item["canonical_url"]),
                            )
                            for item in cursor.fetchall()
                        }
                    seen_text: set[str] = set()
                    kept: list[dict[str, Any]] = []
                    for item in candidates:
                        quote, url = texts.get(str(item["claimId"]), ("", ""))
                        # Two claim ids over one span are one quotation to a reader,
                        # and offering both spends a choice on nothing.
                        if not quote or quote in seen_text:
                            continue
                        seen_text.add(quote)
                        kept.append({**item, "quote": quote[:600], "url": url})
                    candidates = kept
                detail.append(
                    {
                        "conceptClaimId": statement_id,
                        "statement": statement[:300],
                        "topicIds": sorted({str(value) for value in row["topic_ids"]}),
                        "candidates": candidates,
                        # The queue the owner votes in reads these two keys, and
                        # what they should now vote on is the in-topic pair.
                        "lexicalTop": (
                            {"claimId": answers["lexicalInTopic"][0]}
                            if answers["lexicalInTopic"]
                            else None
                        ),
                        "semanticTop": (
                            {
                                "claimId": str(semantic_in_topic[0]["claim_id"]),
                                "score": round(float(semantic_in_topic[0]["score"]), 4),
                                "quote": str(semantic_in_topic[0]["quote_text"])[:300],
                            }
                            if semantic_in_topic
                            else None
                        ),
                        "lexicalTopAnywhere": answers["lexical"][:1],
                        "semanticTopAnywhere": answers["semantic"][:1],
                        "overlapInTopic": len(scoped_shared),
                        "overlapAnywhere": len(open_shared),
                    }
                )

            total = len(statements)
            summary = {
                "model": model_id,
                "top": top,
                "statements": total,
                "statementsWithTopic": total,
                "runs": {
                    name: {
                        "statementsAnswered": hit[name],
                        "candidatesReturned": returned[name],
                        "firstChoiceOnTopic": on_topic_first[name],
                        "firstChoiceOnTopicShare": (
                            round(on_topic_first[name] / hit[name], 3) if hit[name] else 0
                        ),
                        "candidatesOnTopicShare": (
                            round(on_topic_all[name] / returned[name], 3) if returned[name] else 0
                        ),
                    }
                    for name in runs
                },
                "sameFirstChoiceAnywhere": agree_open,
                "sameFirstChoiceInTopic": agree_scoped,
                "averageOverlapAnywhere": round(overlap_open / total, 3) if total else 0,
                "averageOverlapInTopic": round(overlap_scoped / total, 3) if total else 0,
            }
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO kx.binding_method_comparisons"
                    " (ran_by, statements, summary, detail, notes) VALUES (%s, %s, %s, %s, %s)"
                    " RETURNING comparison_id",
                    (
                        ran_by,
                        total,
                        Jsonb(summary),
                        Jsonb(detail),
                        "both methods run live, over everything and inside the statement's"
                        " level-1 subject (authored backbone)",
                    ),
                )
                comparison_id = str(one_row(cursor)["comparison_id"])
        return {"comparisonId": comparison_id, **summary}

    def _latest_comparison(self) -> list[dict[str, Any]]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT detail FROM kx.binding_method_comparisons ORDER BY ran_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
            return [] if row is None else cast(list[dict[str, Any]], row["detail"])

    def method_comparison_queue(self, *, limit: int = 25) -> tuple[int, list[dict[str, Any]]]:
        """One statement and the five quotations offered for it, in a blind order.

        The two methods are interleaved into one shortlist and then ordered by a
        hash of the statement and the quotation together, so position says nothing
        about which method found it or how highly that method ranked it. Which
        method a chosen quotation came from is resolved afterwards, from the
        recorded comparison, and is never sent to the browser.
        """
        detail = self._latest_comparison()
        if not detail:
            return 0, []
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT concept_claim_id::text AS id FROM kx.binding_method_votes")
            voted = {str(item["id"]) for item in cursor.fetchall()}

        # A statement with one candidate is not a choice; it is shown when a second
        # exists, and counted as waiting only then.
        waiting = [
            item
            for item in detail
            if str(item["conceptClaimId"]) not in voted
            and len(cast(list[dict[str, Any]], item.get("candidates") or [])) > 1
        ]
        page: list[dict[str, Any]] = []
        for item in waiting[:limit]:
            statement_id = str(item["conceptClaimId"])
            candidates = cast(list[dict[str, Any]], item["candidates"])
            page.append(
                {
                    "conceptClaimId": statement_id,
                    "statement": str(item["statement"]),
                    "candidates": [
                        {
                            "claimId": str(candidate["claimId"]),
                            "quote": str(candidate.get("quote") or ""),
                            "sourceUrl": str(candidate.get("url") or ""),
                        }
                        for candidate in sorted(
                            candidates,
                            key=lambda candidate: sha256_bytes(
                                f"{statement_id}{candidate['claimId']}".encode()
                            ),
                        )
                    ],
                }
            )
        return len(waiting), page

    def record_candidate_vote(
        self, *, concept_claim_id: str, claim_id: str | None, voted_by: str
    ) -> dict[str, Any]:
        """Record which of the five a person chose, and what that says about the methods.

        The choice is a claim id; which method produced it, and at what rank, is
        looked up here rather than asked of the person - they were never told.
        """
        entry = next(
            (
                item
                for item in self._latest_comparison()
                if str(item["conceptClaimId"]) == concept_claim_id
            ),
            None,
        )
        candidates = cast(list[dict[str, Any]], (entry or {}).get("candidates") or [])
        chosen = next((item for item in candidates if str(item["claimId"]) == claim_id), None)
        if claim_id is not None and chosen is None:
            raise ValueError(f"{claim_id} was not offered for this statement")

        def top(method: str) -> str | None:
            first = next((item for item in candidates if item.get(f"{method}Rank") == 1), None)
            return str(first["claimId"]) if first else None

        if chosen is None:
            winner, lexical_id, semantic_id = "neither", top("lexical"), top("semantic")
            ranks: dict[str, Any] = {}
        else:
            by_lexical = chosen.get("lexicalRank") is not None
            by_semantic = chosen.get("semanticRank") is not None
            winner = (
                "both" if by_lexical and by_semantic else "lexical" if by_lexical else "semantic"
            )
            lexical_id = str(chosen["claimId"]) if by_lexical else None
            semantic_id = str(chosen["claimId"]) if by_semantic else None
            ranks = {
                "lexicalRank": chosen.get("lexicalRank"),
                "semanticRank": chosen.get("semanticRank"),
            }
        outcome = self.record_method_vote(
            concept_claim_id=concept_claim_id,
            winner=winner,
            lexical_claim_id=lexical_id,
            semantic_claim_id=semantic_id,
            voted_by=voted_by,
        )
        return {**outcome, **ranks}

    def record_method_vote(
        self,
        *,
        concept_claim_id: str,
        winner: str,
        lexical_claim_id: str | None,
        semantic_claim_id: str | None,
        voted_by: str,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO kx.binding_method_votes"
                    " (concept_claim_id, winner, lexical_claim_id, semantic_claim_id, voted_by)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (concept_claim_id, winner, lexical_claim_id, semantic_claim_id, voted_by),
                )
                # A vote is immutable and one per person per statement, so a second
                # one changes nothing. Saying so beats returning the same success.
                recorded = cursor.rowcount == 1
        return {"conceptClaimId": concept_claim_id, "winner": winner, "recorded": recorded}

    def method_vote_tally(self) -> dict[str, Any]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT winner, count(*) AS total FROM kx.binding_method_votes GROUP BY 1"
            )
            return {str(row["winner"]): int(cast(int, row["total"])) for row in cursor.fetchall()}

    def coverage_report(self) -> dict[str, Any]:
        """Counts per membership class, plus the full-text smoke gate.

        The classes are reported separately and never summed: coverage over the
        issue perimeter, over the whole corpus and over the canon answer three
        different questions, and a single percentage over their union would be
        meaningless (corpus-membership contract §9).
        """
        report: dict[str, Any] = {"scopes": {}, "smoke": [], "gate": {}}
        with self.connect() as connection:
            self.require_schema(connection)
            with connection.cursor() as cursor:
                for scope, documents in SCOPES.items():
                    cursor.execute(
                        f"""
                        WITH scope_documents AS ({documents})
                        SELECT count(DISTINCT documents.document_id) AS documents,
                               count(DISTINCT versions.document_id)
                                   FILTER (WHERE versions.is_complete) AS complete_documents,
                               count(versions.version_id)
                                   FILTER (WHERE versions.is_complete) AS complete_versions,
                               coalesce(sum(length(versions.canonical_text))
                                   FILTER (WHERE versions.is_complete), 0) AS characters
                        FROM scope_documents
                        JOIN kx.documents USING (document_id)
                        LEFT JOIN kx.document_versions AS versions USING (document_id)
                        """  # noqa: S608 - `documents` is a constant from SCOPES, never input
                    )
                    counts = dict(one_row(cursor))
                    cursor.execute(
                        f"""
                        WITH scope_documents AS ({documents})
                        SELECT count(*) AS chunks
                        FROM kx.chunks
                        JOIN kx.document_versions AS versions USING (version_id)
                        JOIN scope_documents USING (document_id)
                        WHERE versions.is_complete
                        """  # noqa: S608 - same constant
                    )
                    counts["chunks"] = one_row(cursor)["chunks"]
                    cursor.execute(
                        f"""
                        WITH scope_documents AS ({documents}),
                        best AS (
                            SELECT DISTINCT ON (versions.document_id)
                                   versions.document_id, versions.canonical_text_sha256
                            FROM kx.document_versions AS versions
                            JOIN scope_documents USING (document_id)
                            WHERE versions.is_complete
                            ORDER BY versions.document_id, versions.fetched_at DESC
                        )
                        -- count the documents in a shared group, not the group size
                        -- once per member: joining a nine-member group back onto its
                        -- own rows sums to eighty-one, which is not a count of
                        -- anything.
                        SELECT count(DISTINCT canonical_text_sha256) AS distinct_texts,
                               count(*) FILTER (WHERE shared.canonical_text_sha256 IS NOT NULL)
                                   AS sharing
                        FROM best
                        LEFT JOIN (
                            SELECT canonical_text_sha256
                            FROM best GROUP BY 1 HAVING count(*) > 1
                        ) AS shared USING (canonical_text_sha256)
                        """  # noqa: S608 - same constant
                    )
                    # A document whose text another document already carries adds no
                    # evidence, and nine perimeter documents share one 215-character
                    # page footer. Counted here so the number is watched rather than
                    # rediscovered.
                    duplicates = one_row(cursor)
                    counts["distinct_texts"] = duplicates["distinct_texts"]
                    counts["documents_sharing_a_text"] = duplicates["sharing"]
                    total = int(counts["documents"])
                    complete = int(counts["complete_documents"])
                    counts["completeShare"] = (complete / total) if total else 0.0
                    report["scopes"][scope] = counts

                for query, configuration, floor in SMOKE_QUERIES:
                    cursor.execute(
                        """
                        SELECT count(*) AS chunks
                        FROM kx.chunks
                        WHERE to_tsvector(%s::regconfig, text)
                              @@ websearch_to_tsquery(%s::regconfig, %s)
                        """,
                        (configuration, configuration, query),
                    )
                    found = int(one_row(cursor)["chunks"])
                    report["smoke"].append(
                        {
                            "query": query,
                            "configuration": configuration,
                            "floor": floor,
                            "chunks": found,
                            "ok": found >= floor,
                        }
                    )

        perimeter = report["scopes"]["current"]
        report["gate"] = {
            # An empty perimeter is not a complete one. Vacuous truth here would
            # report a green gate on a store that holds nothing.
            "perimeterFullTextComplete": int(perimeter["documents"]) > 0
            and perimeter["documents"] == perimeter["complete_documents"],
            "smokeQueriesMeetTheirFloor": all(item["ok"] for item in report["smoke"]),
        }
        report["status"] = "ok" if all(report["gate"].values()) else "failed"
        return report

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
