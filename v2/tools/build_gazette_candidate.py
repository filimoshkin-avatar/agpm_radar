"""Build one gazette candidate for any period, new issue or replacement.

The predecessor of this tool had `2026-08`, that month's title and the
`present` precondition written into it, because it existed to update one
accepted August asset once. A new issue therefore could not be published at
all, and every September revision reached the reader as a file bundled into
the application: five of them by 2026-09-05, each needing an edit in three
separate lists and a full application deploy.

What this reads from the database instead of being told: whether the period
already has a gazette, its id and its current content hash. What it reads from
the HTML: the title. The validator requires the candidate title and the
document's own `<title>` to be the same string, so deriving it removes the one
way that check could ever be made to fail by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from pathlib import Path

from packages.delta.engine import inspect_release_database
from packages.domain.candidate_package import build_candidate_package
from packages.domain.snapshot import JsonObject, canonical_json_line
from packages.legacy_bridge.importer import deterministic_id
from packages.storage.safe_files import atomic_write_new
from packages.validation.gazette import validate_gazette_candidate

_PERIOD = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def html_title(content: bytes) -> str:
    """The document's own title, normalised the way the validator normalises it."""
    found = _TITLE.search(content.decode("utf-8"))
    if found is None:
        raise ValueError("gazette HTML has no <title>")
    title = " ".join(found.group(1).split())
    if not title:
        raise ValueError("gazette HTML title is empty")
    return title


def current_gazette(database: Path, period: str) -> tuple[str, str] | None:
    """The published gazette of this period as (id, content hash), or None."""
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            """
            SELECT gazette_id, content_hash
            FROM gazettes
            WHERE period = ? AND lifecycle_status = 'published'
            """,
            (period,),
        ).fetchone()
    return (str(row[0]), str(row[1])) if row is not None else None


def build(
    *,
    source_db: Path,
    html: Path,
    period: str,
    candidate_id: str,
    created_at: str,
    root: Path,
) -> JsonObject:
    """Package one gazette candidate; the period decides new issue or replacement."""
    if _PERIOD.fullmatch(period) is None:
        raise ValueError(f"period is not YYYY-MM: {period}")
    content = html.read_bytes()
    title = html_title(content)
    digest = hashlib.sha256(content).hexdigest()
    # The asset URL carries the bytes it serves. `/gazettes/<период>/` is answered
    # with `public, max-age=31536000, immutable`, so a revision at the old path
    # would never reach a returning reader - and the activator refuses it outright
    # ("immutable asset path contains different bytes"), which is how this rule
    # was found on 2026-09-05. Same reasoning as `?v=` on app.mjs: the token is
    # the file, not a number somebody remembers to change.
    relative = f"gazettes/{period}/index-{digest[:12]}.html"
    existing = current_gazette(source_db, period)
    gazette_id = existing[0] if existing is not None else deterministic_id("gazette", period)
    expected: JsonObject = (
        {"state": "present", "contentHash": existing[1]}
        if existing is not None
        else {"state": "absent"}
    )
    base = inspect_release_database(source_db)
    candidate: JsonObject = {
        "candidateId": candidate_id,
        "contractVersion": "1.0.0",
        "createdAt": created_at,
        "expectedBase": {
            "logicalStateHash": base.digest.state_hash,
            "releaseId": base.release.release_id,
            "sequence": base.release.sequence,
        },
        "expectedGazette": expected,
        "gazetteId": gazette_id,
        "htmlEntrypoint": relative,
        "idempotencyKey": "idem_" + candidate_id,
        "initiator": {"actorId": "project-manager", "kind": "project-manager", "requestId": None},
        "inputAssets": [
            {
                "bytes": len(content),
                "mediaType": "text/html",
                "relativePath": relative,
                "sha256": digest,
            }
        ],
        "llmOutcome": {
            "attempts": [],
            "deterministicFallback": None,
            "effective": None,
            "effectiveAttemptOrder": None,
            "requested": None,
            "status": "not_requested",
        },
        "operation": "gazette",
        "ownerRequestDigest": digest,
        "period": period,
        "reason": (
            f"gazette {period}: "
            + ("replace the published asset" if existing is not None else "first published issue")
        ),
        "schemaVersion": 1,
        "title": title,
    }
    # Validated here as well as inside the package builder: a rejection that
    # names the file is worth more than one that names a staging path.
    report = validate_gazette_candidate(candidate, {relative: content})
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    staging = root / "staging"
    packages = root / "packages"
    staging.mkdir(mode=0o700)
    packages.mkdir(mode=0o700)
    atomic_write_new(root / "candidate.json", canonical_json_line(candidate), mode=0o600)
    result = build_candidate_package(
        source_database=source_db,
        staging_database=staging / "gazette.sqlite",
        package_store=packages,
        candidate=candidate,
        assets={relative: content},
    )
    return {
        "candidate": str(root / "candidate.json"),
        "entrypoint": relative,
        "externalLinks": report.external_link_count,
        "gazetteId": gazette_id,
        "operationKind": "replacement" if existing is not None else "first",
        "package": str(result.package.path),
        "packageSha256": result.package.package_sha256,
        "period": period,
        "staging": str(staging / "gazette.sqlite"),
        "title": title,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--period", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    print(
        canonical_json_line(
            build(
                source_db=args.source_db,
                html=args.html,
                period=args.period,
                candidate_id=args.candidate_id,
                created_at=args.created_at,
                root=args.root,
            )
        ).decode(),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
