"""Title policy, independent V2 gates and the actual Legacy pipeline regression."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from packages.contracts.title_quality import (
    TitleQualityError,
    extract_title_candidates,
    reliable_page_title,
    repair_title_reference,
    resolve_title,
    title_problem,
    title_reference_problem,
)
from packages.domain.candidates import CandidateValidationError, _validate_material
from packages.validation.public_issue import (
    PublicIssueValidationError,
    validate_public_issue_document,
)
from tools.build_stage14_daily import _material

ROOT = Path(__file__).resolve().parents[1]
CORA = (
    "Cora Systems shows PMOs how to replace quarterly risk reviews with continuous "
    "portfolio intelligence"
)


@pytest.mark.parametrize(
    "value",
    [
        "User",
        "USER",
        " user ",
        "\tUsEr\n",
        "User:",
        "[Assistant]",
        " SYSTEM!!! ",
        "<|im_start|>user",
        "role: developer",
        "Tool",
        "Function",
        "Human",
        "Пользователь",
        "«Ассистент»",
    ],
)
def test_dialogue_roles_are_normalized(value: str) -> None:
    assert title_problem(value) == "dialogue_role"


@pytest.mark.parametrize(
    "value", ["", "  ", None, "No title", "N/A", "...", "123", "xxx", "https://example.org/story"]
)
def test_missing_and_technical_titles_are_rejected(value: object) -> None:
    assert title_problem(value)
    with pytest.raises(TitleQualityError, match="TITLE_QUALITY_GATE.*no_reliable_html_title"):
        resolve_title(value, "<html>unavailable</html>", "https://example.org/story")


@pytest.mark.parametrize("value", ["AI", "PMO", "System design", "User experience", "Go", "Риск"])
def test_short_meaningful_titles_are_valid(value: str) -> None:
    assert title_problem(value) is None
    assert resolve_title(value, f'<meta property="og:title" content="{value}">').title == value


def test_real_cora_metadata_recovers_user() -> None:
    markup = (ROOT / "tests/data/title-cora.html").read_text()
    result = resolve_title("User", markup)
    assert result.title == CORA
    assert result.source == "jsonld:headline"
    assert result.reason == "dialogue_role"


@pytest.mark.parametrize(
    "markup,source",
    [
        ('<meta content="Risk &amp; control" property="og:title">', "og:title"),
        (
            '<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","headline":"Risk & control"}]}</script>',
            "jsonld:headline",
        ),
        (
            '<script type="application/ld+json">{"@type":"Article","name":"Risk & control"}</script>',
            "jsonld:name",
        ),
    ],
)
def test_available_reliable_metadata_recovers_short_role(markup: str, source: str) -> None:
    result = resolve_title("Assistant", markup)
    assert result.title == "Risk & control"
    assert result.source == source


def test_ranking_ignores_document_order_and_unrelated_organization_name() -> None:
    tags = [
        "<title>Wrong page title</title>",
        "<h1>Navigation menu</h1>",
        '<meta name="twitter:title" content="Different social title">',
        '<meta property="og:title" content="Different Open Graph title">',
        '<script type="application/ld+json">[{"@type":"Organization","name":"Publisher brand"}, {"@type":"Article","headline":"Original article headline"}]</script>',
    ]
    for markup in ("".join(tags), "".join(reversed(tags))):
        result = resolve_title("User", markup)
        assert result.title == "Original article headline"
        assert result.source == "jsonld:headline"


def test_duplicates_do_not_vote_but_independent_agreement_breaks_same_rank_tie() -> None:
    markup = '<meta property="og:title" content="First headline">' * 20
    markup += '<meta property="og:title" content="Second headline">'
    assert reliable_page_title(markup) is None
    assert len(extract_title_candidates(markup)) == 2
    with pytest.raises(TitleQualityError):
        resolve_title("System", markup)
    markup += '<meta name="twitter:title" content="Second headline">'
    assert resolve_title("System", markup).title == "Second headline"


def test_low_confidence_html_and_non_article_jsonld_cannot_repair() -> None:
    for markup in [
        "<h1>Navigation</h1>",
        "<title>Publisher home</title>",
        '<script type="application/ld+json">{"@type":"Organization","name":"Company"}</script>',
        '<meta property="og:title" content="Access denied">',
    ]:
        with pytest.raises(TitleQualityError):
            resolve_title("User", markup)


def test_known_site_suffix_and_agreement_allow_repair() -> None:
    markup = '<meta property="og:site_name" content="Example"><title>Risk &amp; control | Example</title><h1>Risk & control</h1>'
    assert resolve_title("User", markup).title == "Risk & control"
    assert resolve_title("Risk & control", markup).reason is None


def test_mismatch_includes_short_titles() -> None:
    result = resolve_title(
        "Cloud costs", '<meta property="og:title" content="Portfolio risk intelligence">'
    )
    assert result.reason == "page_title_mismatch"
    assert result.title == "Portfolio risk intelligence"


def test_repairs_only_bound_references_and_preserves_good_llm_text() -> None:
    assert (
        repair_title_reference("Материал „User“ описывает PMO.", "User", CORA)
        == f"Материал „{CORA}“ описывает PMO."
    )
    good = "The user approves a proposed portfolio change."
    assert repair_title_reference(good, "User", CORA) == good
    assert title_reference_problem("Материал „User“ описывает PMO.") == "stale_title_reference"
    assert title_reference_problem(good) is None


def test_v2_builder_rejects_upstream_bypass_and_stale_derived_text() -> None:
    with pytest.raises(TitleQualityError, match="dialogue_role"):
        _material({"title": "User", "url": "https://example.org/story"}, 1)
    with pytest.raises(TitleQualityError, match="stale_title_reference"):
        _material(
            {
                "title": CORA,
                "url": "https://example.org/story",
                "brief": "Материал „User“ описывает PMO.",
            },
            1,
        )


def test_domain_and_public_gates_reject_without_mutating_documents() -> None:
    fixture = json.loads(
        (
            ROOT.parent / "fixtures/legacy-baseline/deterministic-fallback-2026-08-15.json"
        ).read_text()
    )
    material = _material(fixture["materials"][0], 1)
    material["title"] = "System:"
    before_material = copy.deepcopy(material)
    with pytest.raises(CandidateValidationError, match="dialogue_role"):
        _validate_material(material, 0, {"effective": None})
    assert material == before_material
    golden = json.loads((ROOT / "fixtures/synthetic/stage6-golden.json").read_text())
    document = next(
        row["document"]
        for row in golden.values()
        if isinstance(row, dict) and row.get("document", {}).get("materials")
    )
    validate_public_issue_document(document)
    before = copy.deepcopy(document)
    document["materials"][0]["title"] = "Assistant"
    with pytest.raises(PublicIssueValidationError, match="dialogue_role"):
        validate_public_issue_document(document)
    document["materials"][0]["title"] = before["materials"][0]["title"]
    assert document == before


def test_v2_transfer_preserves_all_fields_except_a_known_title_repair() -> None:
    raw = cast(
        dict[str, object],
        json.loads(
            (
                ROOT.parent / "fixtures/legacy-baseline/deterministic-fallback-2026-08-15.json"
            ).read_text()
        )["materials"][0],
    )
    expected = _material(raw, 3)
    original = copy.deepcopy(raw)
    raw["title"] = CORA
    actual = _material(raw, 3)
    assert actual == expected | {"title": CORA}
    assert raw == original | {"title": CORA}


def test_legacy_pipeline_regressions() -> None:
    completed = subprocess.run(  # noqa: S603
        [
            "/usr/bin/python3",
            "-m",
            "unittest",
            "discover",
            "-s",
            str(ROOT.parent / "pipeline/tests"),
            "-p",
            "test_title_quality.py",
            "-v",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Ran " in completed.stderr and "OK" in completed.stderr


@pytest.mark.parametrize(
    "title",
    ["403 Forbidden", "404 Not Found", "502 Bad Gateway", "Error 404", "Please enable JavaScript"],
)
def test_soft_error_page_cannot_supply_replacement(title: str) -> None:
    with pytest.raises(TitleQualityError):
        resolve_title("User", f'<meta property="og:title" content="{title}">')


def test_suffix_removal_preserves_all_headline_segments() -> None:
    markup = '<meta property="og:site_name" content="Example"><meta property="og:title" content="Portfolio intelligence - Risk reviews - Example">'
    assert resolve_title("User", markup).title == "Portfolio intelligence - Risk reviews"


def test_public_gate_rejects_stale_issue_analysis_reference() -> None:
    golden = json.loads((ROOT / "fixtures/synthetic/stage6-golden.json").read_text())
    document = next(
        row["document"]
        for row in golden.values()
        if isinstance(row, dict) and row.get("document", {}).get("materials")
    )
    for field in ["brief", "analysis"]:
        changed = copy.deepcopy(document)
        if field == "analysis":
            changed["analysis"]["brief"] = "Материал „User“ описывает PMO."
        else:
            changed[field] = "Материал „User“ описывает PMO."
        with pytest.raises(PublicIssueValidationError, match="stale_title_reference"):
            validate_public_issue_document(changed)
