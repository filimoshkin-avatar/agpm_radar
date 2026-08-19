"""Dependency-free, byte-stable DOCX rendering for one Radar V2 daily issue."""

from __future__ import annotations

import io
import sqlite3
import stat
import zipfile
from collections.abc import Mapping
from typing import Final, cast
from xml.etree import ElementTree as ET

from packages.validation.public_issue import (
    build_public_issue,
    validate_public_issue_document,
)

_W: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT: Final = "http://schemas.openxmlformats.org/package/2006/content-types"
_CP: Final = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC: Final = "http://purl.org/dc/elements/1.1/"
_DCTERMS: Final = "http://purl.org/dc/terms/"
_DCMITYPE: Final = "http://purl.org/dc/dcmitype/"
_XSI: Final = "http://www.w3.org/2001/XMLSchema-instance"
_EP: Final = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_VT: Final = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_XML: Final = "http://www.w3.org/XML/1998/namespace"
_FIXED_ZIP_TIME: Final = (1980, 1, 1, 0, 0, 0)

ET.register_namespace("w", _W)
ET.register_namespace("r", _R)
ET.register_namespace("cp", _CP)
ET.register_namespace("dc", _DC)
ET.register_namespace("dcterms", _DCTERMS)
ET.register_namespace("dcmitype", _DCMITYPE)
ET.register_namespace("xsi", _XSI)
ET.register_namespace("ep", _EP)
ET.register_namespace("vt", _VT)


def _q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _xml(root: ET.Element) -> bytes:
    return cast(
        bytes,
        ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=True),
    )


def _run(paragraph: ET.Element, text: str, *, bold: bool = False) -> None:
    run = ET.SubElement(paragraph, _q(_W, "r"))
    if bold:
        properties = ET.SubElement(run, _q(_W, "rPr"))
        ET.SubElement(properties, _q(_W, "b"))
    node = ET.SubElement(run, _q(_W, "t"))
    if text != text.strip() or "  " in text:
        node.set(_q(_XML, "space"), "preserve")
    node.text = text


def _paragraph(
    body: ET.Element,
    text: str,
    *,
    style: str | None = None,
    bold: bool = False,
) -> ET.Element:
    paragraph = ET.SubElement(body, _q(_W, "p"))
    if style is not None:
        properties = ET.SubElement(paragraph, _q(_W, "pPr"))
        ET.SubElement(properties, _q(_W, "pStyle"), {_q(_W, "val"): style})
    _run(paragraph, text, bold=bold)
    return paragraph


def _hyperlink_paragraph(
    body: ET.Element,
    *,
    prefix: str,
    text: str,
    relationship_id: str,
) -> None:
    paragraph = ET.SubElement(body, _q(_W, "p"))
    _run(paragraph, prefix)
    hyperlink = ET.SubElement(paragraph, _q(_W, "hyperlink"), {_q(_R, "id"): relationship_id})
    run = ET.SubElement(hyperlink, _q(_W, "r"))
    properties = ET.SubElement(run, _q(_W, "rPr"))
    ET.SubElement(properties, _q(_W, "rStyle"), {_q(_W, "val"): "Hyperlink"})
    node = ET.SubElement(run, _q(_W, "t"))
    node.text = text


def _content_types() -> bytes:
    root = ET.Element(_q(_CT, "Types"))
    ET.SubElement(
        root,
        _q(_CT, "Default"),
        {
            "ContentType": "application/vnd.openxmlformats-package.relationships+xml",
            "Extension": "rels",
        },
    )
    ET.SubElement(root, _q(_CT, "Default"), {"ContentType": "application/xml", "Extension": "xml"})
    for part, content_type in (
        (
            "/docProps/app.xml",
            "application/vnd.openxmlformats-officedocument.extended-properties+xml",
        ),
        ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
        (
            "/word/document.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        ),
        (
            "/word/styles.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
        ),
    ):
        ET.SubElement(
            root,
            _q(_CT, "Override"),
            {"ContentType": content_type, "PartName": part},
        )
    return _xml(root)


def _root_relationships() -> bytes:
    root = ET.Element(_q(_PR, "Relationships"))
    for relationship_id, target, relationship_type in (
        (
            "rId1",
            "word/document.xml",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        ),
        (
            "rId2",
            "docProps/core.xml",
            "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
        ),
        (
            "rId3",
            "docProps/app.xml",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
        ),
    ):
        ET.SubElement(
            root,
            _q(_PR, "Relationship"),
            {"Id": relationship_id, "Target": target, "Type": relationship_type},
        )
    return _xml(root)


def _styles() -> bytes:
    root = ET.Element(_q(_W, "styles"))
    for style_id, name, style_type, size, bold in (
        ("Normal", "Normal", "paragraph", "22", False),
        ("Title", "Title", "paragraph", "36", True),
        ("Heading1", "heading 1", "paragraph", "30", True),
        ("Heading2", "heading 2", "paragraph", "26", True),
        ("Hyperlink", "Hyperlink", "character", "22", False),
    ):
        style = ET.SubElement(
            root,
            _q(_W, "style"),
            {_q(_W, "styleId"): style_id, _q(_W, "type"): style_type},
        )
        ET.SubElement(style, _q(_W, "name"), {_q(_W, "val"): name})
        if style_id == "Hyperlink":
            run_properties = ET.SubElement(style, _q(_W, "rPr"))
            ET.SubElement(run_properties, _q(_W, "color"), {_q(_W, "val"): "0563C1"})
            ET.SubElement(run_properties, _q(_W, "u"), {_q(_W, "val"): "single"})
            continue
        run_properties = ET.SubElement(style, _q(_W, "rPr"))
        ET.SubElement(run_properties, _q(_W, "sz"), {_q(_W, "val"): size})
        ET.SubElement(run_properties, _q(_W, "szCs"), {_q(_W, "val"): size})
        if bold:
            ET.SubElement(run_properties, _q(_W, "b"))
    return _xml(root)


def _core_properties(document: Mapping[str, object]) -> bytes:
    root = ET.Element(_q(_CP, "coreProperties"))
    ET.SubElement(root, _q(_DC, "title")).text = cast(str, document["title"])
    ET.SubElement(root, _q(_DC, "subject")).text = "Radar V2 daily issue"
    ET.SubElement(root, _q(_DC, "creator")).text = "Radar V2 deterministic renderer"
    ET.SubElement(root, _q(_CP, "lastModifiedBy")).text = "Radar V2 deterministic renderer"
    timestamp = document["publishedAt"] or f"{document['issueDate']}T00:00:00Z"
    for local in ("created", "modified"):
        node = ET.SubElement(root, _q(_DCTERMS, local))
        node.set(_q(_XSI, "type"), "dcterms:W3CDTF")
        node.text = cast(str, timestamp)
    return _xml(root)


def _app_properties() -> bytes:
    root = ET.Element(_q(_EP, "Properties"))
    ET.SubElement(root, _q(_EP, "Application")).text = "Radar V2"
    ET.SubElement(root, _q(_EP, "AppVersion")).text = "1.0"
    return _xml(root)


def _document_xml(document: Mapping[str, object]) -> tuple[bytes, tuple[tuple[str, str], ...]]:
    root = ET.Element(_q(_W, "document"))
    body = ET.SubElement(root, _q(_W, "body"))
    _paragraph(body, cast(str, document["title"]), style="Title")
    issue_number = document["issueNumber"]
    number_text = f" · выпуск № {issue_number}" if issue_number is not None else ""
    _paragraph(body, f"Дата выпуска: {document['issueDate']}{number_text}")
    if document["brief"]:
        _paragraph(body, cast(str, document["brief"]), bold=True)

    llm = cast(dict[str, object], document["llm"])
    effective = llm["effectiveModel"] or "детерминированный fallback"
    _paragraph(body, f"LLM: {llm['status']} · {effective}")
    if llm["status"] == "unavailable":
        _paragraph(
            body,
            "Предупреждение: все LLM-провайдеры недоступны; "
            "опубликовано детерминированное представление.",
            bold=True,
        )

    stats = cast(dict[str, object], document["stats"])
    _paragraph(body, "Статистика", style="Heading1")
    _paragraph(
        body,
        (
            f"Просмотрено: {stats['viewed']}; включено: {stats['included']}; "
            f"отсечено: {stats['cut']}; ближний/средний/дальний: "
            f"{stats['near']}/{stats['mid']}/{stats['far']}; "
            f"ядро/смежные: {stats['core']}/{stats['adjacent']}."
        ),
    )

    analysis = cast(dict[str, object], document["analysis"])
    _paragraph(body, "Анализ", style="Heading1")
    if analysis["headline"]:
        _paragraph(body, cast(str, analysis["headline"]), bold=True)
    if analysis["brief"] and analysis["brief"] != document["brief"]:
        _paragraph(body, cast(str, analysis["brief"]))
    for raw_block in cast(list[object], analysis["blocks"]):
        block = cast(dict[str, object], raw_block)
        _paragraph(body, cast(str, block["title"]), style="Heading2")
        _paragraph(body, cast(str, block["text"]))

    theses = cast(list[object], document["theses"])
    if theses:
        _paragraph(body, "Тезисы", style="Heading1")
        for raw_thesis in theses:
            thesis = cast(dict[str, object], raw_thesis)
            rest = f" {thesis['rest']}" if thesis["rest"] else ""
            _paragraph(body, f"• {thesis['lead']}{rest}")

    _paragraph(body, "Материалы", style="Heading1")
    materials = cast(list[object], document["materials"])
    relationships: list[tuple[str, str]] = []
    if not materials:
        _paragraph(body, "Квалифицирующих материалов нет.")
    for index, raw_material in enumerate(materials, start=1):
        material = cast(dict[str, object], raw_material)
        _paragraph(body, f"{index}. {material['title']}", style="Heading2")
        relationship_id = f"rIdLink{index:03d}"
        url = cast(str, material["url"])
        relationships.append((relationship_id, url))
        _hyperlink_paragraph(
            body,
            prefix="Источник: ",
            text=url,
            relationship_id=relationship_id,
        )
        source = material["sourceName"] or "не указан"
        published = material["publishedAt"] or "дата не установлена"
        _paragraph(
            body,
            (
                f"Источник: {source}; публикация: {published}; периметр: "
                f"{material['perimeter']}; вердикт: {material['verdict']}; "
                f"сигнал: {material['signalStrength']}."
            ),
        )
        for label, key in (
            ("Кратко", "brief"),
            ("Резюме", "summary"),
            ("Значение для AgPM", "agpmTakeaway"),
            ("Наблюдение", "trendNotes"),
        ):
            if material[key]:
                _paragraph(body, f"{label}: {material[key]}")
        material_theses = cast(list[object], material["theses"])
        for material_thesis in material_theses:
            _paragraph(body, f"• {material_thesis}")

    section = ET.SubElement(body, _q(_W, "sectPr"))
    ET.SubElement(
        section,
        _q(_W, "pgSz"),
        {_q(_W, "h"): "16838", _q(_W, "w"): "11906"},
    )
    ET.SubElement(
        section,
        _q(_W, "pgMar"),
        {
            _q(_W, "bottom"): "1134",
            _q(_W, "footer"): "708",
            _q(_W, "gutter"): "0",
            _q(_W, "header"): "708",
            _q(_W, "left"): "1134",
            _q(_W, "right"): "1134",
            _q(_W, "top"): "1134",
        },
    )
    return _xml(root), tuple(relationships)


def _document_relationships(links: tuple[tuple[str, str], ...]) -> bytes:
    root = ET.Element(_q(_PR, "Relationships"))
    ET.SubElement(
        root,
        _q(_PR, "Relationship"),
        {
            "Id": "rIdStyles",
            "Target": "styles.xml",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
        },
    )
    for relationship_id, url in links:
        ET.SubElement(
            root,
            _q(_PR, "Relationship"),
            {
                "Id": relationship_id,
                "Target": url,
                "TargetMode": "External",
                "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            },
        )
    return _xml(root)


def _zip(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def render_public_issue_docx(document: Mapping[str, object]) -> bytes:
    """Render an exact public IssueDetail into a deterministic, macro-free DOCX."""
    validated = validate_public_issue_document(dict(document))
    document_xml, links = _document_xml(cast(dict[str, object], validated))
    files = {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": _root_relationships(),
        "docProps/app.xml": _app_properties(),
        "docProps/core.xml": _core_properties(validated),
        "word/_rels/document.xml.rels": _document_relationships(links),
        "word/document.xml": document_xml,
        "word/styles.xml": _styles(),
    }
    return _zip(files)


def render_daily_docx(connection: sqlite3.Connection, *, issue_date: str) -> bytes:
    """Validate a published SQLite aggregate and render its daily DOCX."""
    return render_public_issue_docx(build_public_issue(connection, issue_date=issue_date))


__all__ = ["render_daily_docx", "render_public_issue_docx"]
