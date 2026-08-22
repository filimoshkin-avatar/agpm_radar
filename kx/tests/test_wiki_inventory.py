from __future__ import annotations

from pathlib import Path

import pytest

from radar_kx.wiki_inventory import (
    RELATIONSHIP_TYPES,
    SECTION_ALIASES,
    build_register,
    canonical_section,
)

AGPM_SCHEMA = Path("/root/.openclaw-projectmanager/workspace/knowledge/agpm/SCHEMA.md")


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Purpose", "purpose"),
        ("Назначение", "purpose"),
        ("Core claims", "core_claims"),
        ("Ключевые тезисы", "core_claims"),
        ("Supporting sources", "supporting_sources"),
        ("Поддерживающие источники", "supporting_sources"),
        ("Tensions / Contradictions", "tensions"),
        ("Напряжения / противоречия", "tensions"),
        ("Implications for AgPM model", "implications"),
        ("Следствия для модели AgPM", "implications"),
        ("Open questions", "open_questions"),
        ("Открытые вопросы", "open_questions"),
        # Emphasis and trailing punctuation must not defeat the match.
        ("**Open questions**", "open_questions"),
        ("Open questions:", "open_questions"),
        # A heading that is genuinely something else stays unmapped rather than being
        # forced into the nearest convention.
        ("Терминологическое решение", None),
        ("", None),
    ],
)
def test_bilingual_headings_map_onto_the_schema_conventions(
    heading: str, expected: str | None
) -> None:
    assert canonical_section(heading) == expected


def test_schema_conventions_and_relationship_vocabulary_match_the_wiki_schema_file() -> None:
    if not AGPM_SCHEMA.exists():
        pytest.skip("the AgPM wiki is not present on this host")
    schema = AGPM_SCHEMA.read_text(encoding="utf-8").casefold()
    for relationship in RELATIONSHIP_TYPES:
        assert f"`{relationship}`" in schema
    # The English name of every convention still has to appear under Page conventions,
    # otherwise the alias table has drifted away from the schema it claims to follow.
    for english in (
        "purpose",
        "core claims",
        "supporting sources",
        "tensions / contradictions",
        "implications for agpm model",
        "open questions",
    ):
        assert english in schema


@pytest.fixture
def wiki(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "agpm"
    (root / "wiki" / "principles").mkdir(parents=True)
    (root / "wiki" / "sources").mkdir(parents=True)
    (root / "wiki" / "risks").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    (root / "SCHEMA.md").write_text("# Schema\n", encoding="utf-8")
    (root / "raw" / "white-paper.md").write_text(
        "# White paper\n\n## Core claims\n- raw text is not an authored claim\n", encoding="utf-8"
    )
    (root / "wiki" / "principles" / "five.md").write_text(
        "\n".join(
            [
                "# Five principles",
                "",
                "## Назначение",
                "Зачем страница существует.",
                "",
                "## Ключевые тезисы",
                "1. **Explainability**: решение агента должно быть понятно на языке бизнеса.",
                "2. Accountability: решение прослеживается по неизменяемому журналу.",
                "",
                "```",
                "## Not a heading, this is a fence",
                "- not a claim either",
                "```",
                "",
                "## Поддерживающие источники",
                "- White paper: Part II",
                "- См. [заметку](../sources/white-paper.md) и https://manifesto.aipractice.space/",
                "",
                "## Открытые вопросы",
                "- Как это выглядит на уровне зрелости 1?",
            ]
        ),
        encoding="utf-8",
    )
    (root / "wiki" / "sources" / "white-paper.md").write_text(
        "# Source note\n\n## Терминологическое решение\nПрозой, без списков.\n"
        "\nСм. [битую ссылку](../principles/missing.md)\n",
        encoding="utf-8",
    )
    return {"agpm": root}


def test_register_classifies_layers_and_counts_only_authored_pages(wiki: dict[str, Path]) -> None:
    register = build_register(wiki)
    inventory = register["inventory"]
    assert inventory["byLayer"] == {
        "raw_extract": 1,
        "schema": 1,
        "source_note": 1,
        "synthesis_page": 1,
    }
    # The raw extract has a "Core claims" heading with a list item under it, and it must
    # still not be counted: raw is immutable source text, not something we assert.
    assert inventory["totals"]["authoredPages"] == 2
    assert inventory["claimsByLayer"] == {"source_note": 0, "synthesis_page": 3}


def test_atomic_claims_come_from_list_items_under_claim_bearing_sections(
    wiki: dict[str, Path],
) -> None:
    register = build_register(wiki)
    page = next(
        item for item in register["pages"] if item["relativePath"].endswith("principles/five.md")
    )
    assert page["sectionsPresent"] == [
        "core_claims",
        "open_questions",
        "purpose",
        "supporting_sources",
    ]
    assert page["sectionsMissing"] == ["tensions", "implications"]
    assert [claim["section"] for claim in page["claims"]] == [
        "core_claims",
        "core_claims",
        "open_questions",
    ]
    # Emphasis is stripped so the claim text is the assertion, not its markup.
    assert page["claims"][0]["text"].startswith("Explainability: решение агента")
    # Supporting sources are references, not claims, so nothing is harvested from them.
    assert all(claim["text"] != "White paper: Part II" for claim in page["claims"])


def test_fenced_blocks_are_not_read_as_headings_or_claims(wiki: dict[str, Path]) -> None:
    register = build_register(wiki)
    page = next(
        item for item in register["pages"] if item["relativePath"].endswith("principles/five.md")
    )
    headings = [item["text"] for item in page["headings"]]
    assert "Not a heading, this is a fence" not in headings
    assert all(claim["text"] != "not a claim either" for claim in page["claims"])


def test_links_are_resolved_and_broken_targets_are_named(wiki: dict[str, Path]) -> None:
    register = build_register(wiki)
    broken = register["inventory"]["linkGraph"]["brokenLinks"]
    assert broken == [
        {"page": "agpm/wiki/sources/white-paper.md", "target": "../principles/missing.md"}
    ]
    page = next(
        item for item in register["pages"] if item["relativePath"].endswith("principles/five.md")
    )
    assert [link["resolved"] for link in page["internalLinks"]] == [
        "agpm/wiki/sources/white-paper.md"
    ]
    assert page["externalUrls"] == ["https://manifesto.aipractice.space/"]


def test_evidence_posture_separates_pages_that_cite_from_pages_that_only_assert(
    wiki: dict[str, Path],
) -> None:
    register = build_register(wiki)
    evidence = register["inventory"]["evidencePosture"]
    assert evidence["pagesCitingSources"] == 1
    assert evidence["pagesWithoutAnySource"] == ["agpm/wiki/sources/white-paper.md"]


def test_empty_section_directories_are_reported(wiki: dict[str, Path]) -> None:
    register = build_register(wiki)
    assert register["inventory"]["emptySectionDirectories"] == ["agpm/wiki/risks"]


def test_convention_conformance_is_measured_not_assumed(wiki: dict[str, Path]) -> None:
    register = build_register(wiki)
    conventions = register["inventory"]["pageConventions"]
    assert conventions["sectionCoverage"] == {
        "purpose": 1,
        "core_claims": 1,
        "supporting_sources": 1,
        "tensions": 0,
        "implications": 0,
        "open_questions": 1,
    }
    assert conventions["fullyConformantPages"] == []
    assert conventions["pagesWithNoRecognizedSection"] == ["agpm/wiki/sources/white-paper.md"]
    assert len(SECTION_ALIASES) == 6
