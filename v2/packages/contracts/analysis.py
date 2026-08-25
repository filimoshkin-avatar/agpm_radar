"""Contract-level normalisation for the analysis block.

One rule in one place. The builder and the public projection both clean the
LLM's evidence titles, and for a day they did it with two texts that said the
same thing - one returning early on a non-list, one coercing it. Nothing had
drifted yet; the docstring in the projection already claimed «one rule, not
two», which is how drift starts.
"""

from __future__ import annotations


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
