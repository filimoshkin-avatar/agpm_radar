"""Contract-level normalisation for the analysis block.

One rule in one place. The builder and the public projection both clean the
LLM's evidence titles, and for a day they did it with two texts that said the
same thing - one returning early on a non-list, one coercing it. Nothing had
drifted yet; the docstring in the projection already claimed «one rule, not
two», which is how drift starts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence


def clean_evidence_titles(raw: object) -> list[str]:
    """The LLM's own list of source titles, kept as the LLM chose it.

    Order is preserved - it is the analysis's composition, not an index -
    empties are dropped, and duplicates are removed stably: the first mention
    stands, nothing is reordered.
    """
    if not isinstance(raw, list):
        return []
    titles: list[str] = []
    for item in raw:
        title = item.strip() if isinstance(item, str) else ""
        if title and title not in titles:
            titles.append(title)
    return titles


def clean_evidence_material_ids(raw: object) -> list[str]:
    """Return stable, unique V2 material identifiers chosen as evidence."""
    if not isinstance(raw, list):
        return []
    material_ids: list[str] = []
    for item in raw:
        material_id = item.strip() if isinstance(item, str) else ""
        if material_id and material_id not in material_ids:
            material_ids.append(material_id)
    return material_ids


def issue_content_hash(materials: Sequence[Mapping[str, object]]) -> str:
    """Bind generated analysis to the ordered, final V2 issue composition."""
    payload = [
        {
            "materialId": item.get("materialId"),
            "title": item.get("title"),
            "summary": item.get("summary"),
            "rubrics": item.get("rubrics"),
            "perimeter": item.get("perimeter"),
        }
        for item in materials
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
