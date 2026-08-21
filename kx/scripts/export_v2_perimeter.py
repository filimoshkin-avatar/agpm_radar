#!/usr/bin/env python3
"""Export the active Radar V2 issue perimeter as a Radar KX perimeter artifact.

Read-only against Radar V2: the release database is opened with an immutable URI,
so this cannot write to, lock, or otherwise disturb the running V2 API. It uses the
standard library only, so it runs on the production host without any dependency.

Every source row is carried through verbatim under ``issue``/``material``/
``issue_material`` in addition to the flattened editorial projection, so the import
is lossless even where KX has no dedicated column.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MEMBER_QUERY = """
SELECT issue_materials.*,
       issues.issue_date AS issue_date,
       issues.issue_number AS issue_number,
       issues.title AS issue_title,
       materials.title AS material_title,
       materials.url AS material_url,
       materials.canonical_url AS material_canonical_url,
       materials.published_at AS material_published_at,
       materials.summary AS material_summary,
       materials.agpm_takeaway AS material_agpm_takeaway,
       materials.brief AS material_brief
FROM issue_materials
JOIN issues USING (issue_id)
JOIN materials USING (material_id)
ORDER BY issues.issue_date, issue_materials.sort_order, issue_materials.material_id
"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _by_key(connection: sqlite3.Connection, query: str, key: str) -> dict[str, dict[str, Any]]:
    return {str(row[key]): dict(row) for row in connection.execute(query)}


def _json_value(raw: object, fallback: Any) -> Any:
    if not isinstance(raw, str) or not raw:
        return fallback
    try:
        return json.loads(raw)
    except ValueError:
        return fallback


def _member(
    row: dict[str, Any],
    *,
    issues: dict[str, dict[str, Any]],
    materials: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    issue = issues[str(row["issue_id"])]
    material = materials[str(row["material_id"])]
    issue_material = {
        key: value
        for key, value in row.items()
        if key
        not in {
            "issue_date",
            "issue_number",
            "issue_title",
            "material_title",
            "material_url",
            "material_canonical_url",
            "material_published_at",
            "material_summary",
            "material_agpm_takeaway",
            "material_brief",
        }
    }
    url = str(material["url"])
    return {
        "issue_id": issue["issue_id"],
        "issue_date": issue["issue_date"],
        "issue_number": issue["issue_number"],
        "issue_title": issue["title"],
        "material_ref": material["material_id"],
        "sort_order": row["sort_order"],
        "perimeter": row["perimeter"],
        "verdict": row["verdict"],
        "key_material": bool(row["key_material"]),
        "signal_score": row["signal_score"],
        "signal_strength": row["signal_strength"],
        "title": material["title"],
        "source_url": url,
        "canonical_url": material["canonical_url"] or url,
        # The issue-level editorial text overrides the material-level text where the
        # editor wrote one; both survive verbatim in the nested source rows below.
        "summary": row["summary"] or material["summary"],
        "agpm_takeaway": row["agpm_takeaway"] or material["agpm_takeaway"],
        "brief": row["brief"] or material["brief"],
        "trend_notes": row["trend_notes"],
        "theses": _json_value(row["theses_json"], []),
        "flags": _json_value(row["flags_json"], {}),
        "published_at": material["published_at"],
        "issue": issue,
        "material": material,
        "issue_material": issue_material,
    }


def export(*, content_root: Path, output: Path) -> dict[str, Any]:
    active = json.loads((content_root / "active.json").read_text(encoding="utf-8"))
    database_path = (content_root / str(active["database"])).resolve()
    source_sha256 = _sha256_file(database_path)
    connection = sqlite3.connect(f"file:{database_path}?immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        issues = _by_key(connection, "SELECT * FROM issues", "issue_id")
        materials = _by_key(connection, "SELECT * FROM materials", "material_id")
        members = [
            _member(dict(row), issues=issues, materials=materials)
            for row in connection.execute(MEMBER_QUERY)
        ]
    finally:
        connection.close()
    document = {
        "source": {
            "perimeter_source_id": f"v2_content_release:{active['releaseId']}",
            "source_kind": "v2_content_release",
            "source_reference": str(active["database"]),
            "source_sha256": source_sha256,
            "captured_at": datetime.now(UTC).isoformat(),
            "state_hash": active.get("stateHash"),
        },
        "members": members,
    }
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
    output.write_text(payload + "\n", encoding="utf-8")
    return {
        "output": str(output),
        "outputSha256": _sha256_file(output),
        "sourceReference": document["source"]["source_reference"],
        "sourceSha256": source_sha256,
        "memberRows": len(members),
        "issues": len({member["issue_id"] for member in members}),
        "materials": len({member["material_ref"] for member in members}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="export_v2_perimeter")
    parser.add_argument("--content-root", type=Path, default=Path("/var/lib/radar-v2/content"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export(content_root=args.content_root, output=args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
