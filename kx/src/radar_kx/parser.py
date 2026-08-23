from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from radar_kx.identifiers import PARSER_NAME, PARSER_VERSION, canonicalize_text
from radar_kx.language import language_of

PYPDF_LOGGER = logging.getLogger("pypdf")
PYPDF_LOGGER.addHandler(logging.NullHandler())
PYPDF_LOGGER.propagate = False

CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([^;\s\"']+)", re.IGNORECASE)
BLOCK_PAGE_MARKERS = (
    "access denied",
    "attention required",
    "enable javascript and cookies to continue",
    "just a moment",
    "request blocked",
    "you have been blocked",
)
REMIX_ENQUEUE_RE = re.compile(
    r'streamController\.enqueue\(("(?:\\.|[^"\\])*")\)',
    re.DOTALL,
)
MAX_REMIX_ITEMS = 200_000
ARTICLE_LD_TYPES = frozenset(
    {"Article", "BlogPosting", "NewsArticle", "Report", "ScholarlyArticle", "TechArticle"}
)
INLINE_RICH_TEXT_NODES = frozenset({"text", "hyperlink", "entry-hyperlink", "asset-hyperlink"})
MAX_RICH_TEXT_DEPTH = 32

DOCX_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_BODY_ENTRY = "word/document.xml"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
#: A zip entry declares its uncompressed size, so an archive that would expand
#: past this is refused before a byte of it is read.
MAX_DOCX_XML_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ParsedContent:
    text: str
    title: str
    language: str
    parser_name: str
    parser_version: str
    quality: str
    is_complete: bool


def _decode_text(payload: bytes, content_type: str) -> str:
    match = CHARSET_RE.search(content_type)
    candidates = [match.group(1)] if match else []
    candidates.extend(("utf-8", "windows-1251", "latin-1"))
    for encoding in candidates:
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _language(text: str) -> str:
    """Defect D10: this counted Cyrillic against Latin. See radar_kx.language."""
    return language_of(text)


def _title_from_soup(soup: BeautifulSoup) -> str:
    for selector, attribute in (
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
    ):
        element = soup.select_one(selector)
        if element is not None:
            value = element.get(attribute)
            if isinstance(value, str) and value.strip():
                return canonicalize_text(value)[:1000]
    if soup.title is not None:
        return canonicalize_text(soup.title.get_text(" ", strip=True))[:1000]
    return ""


def _telegram_text(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    for selector in (
        ".tgme_widget_message_author_name",
        ".tgme_widget_message_text",
        ".tgme_widget_message_link_preview_title",
        ".tgme_widget_message_link_preview_description",
    ):
        for element in soup.select(selector):
            value = canonicalize_text(element.get_text("\n", strip=True))
            if value and value not in parts:
                parts.append(value)
    return canonicalize_text("\n\n".join(parts))


def _hydrate_remix_value(flat: list[Any], index: int, memo: dict[int, Any]) -> Any:
    if index < 0 or index >= len(flat):
        return None
    if index in memo:
        return memo[index]
    item = flat[index]
    if isinstance(item, dict):
        hydrated: dict[str, Any] = {}
        memo[index] = hydrated
        for raw_key, reference in item.items():
            key = raw_key
            if raw_key.startswith("_") and raw_key[1:].isdigit():
                key_reference = int(raw_key[1:])
                if 0 <= key_reference < len(flat) and isinstance(flat[key_reference], str):
                    key = flat[key_reference]
            hydrated[key] = (
                _hydrate_remix_value(flat, reference, memo) if type(reference) is int else reference
            )
        return hydrated
    if isinstance(item, list):
        hydrated_list: list[Any] = []
        memo[index] = hydrated_list
        hydrated_list.extend(
            _hydrate_remix_value(flat, reference, memo) if type(reference) is int else reference
            for reference in item
        )
        return hydrated_list
    memo[index] = item
    return item


def _remix_article(html: str) -> tuple[str, str]:
    for match in REMIX_ENQUEUE_RE.finditer(html):
        try:
            encoded_payload = json.loads(match.group(1))
            flat = json.loads(encoded_payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(flat, list) or not flat or len(flat) > MAX_REMIX_ITEMS:
            continue
        root = _hydrate_remix_value(flat, 0, {})
        if not isinstance(root, Mapping):
            continue
        loader_data = root.get("loaderData")
        if not isinstance(loader_data, Mapping):
            continue
        route = loader_data.get("routes/$slug")
        if not isinstance(route, Mapping):
            continue
        post = route.get("post")
        if not isinstance(post, Mapping):
            continue
        content = post.get("content")
        if not isinstance(content, str):
            continue
        article_soup = BeautifulSoup(content, "html.parser")
        text = canonicalize_text(article_soup.get_text("\n", strip=True))
        raw_title = post.get("title")
        title = canonicalize_text(raw_title)[:1000] if isinstance(raw_title, str) else ""
        if text:
            return text, title
    return "", ""


def _text_from_markup(value: str) -> str:
    if "<" in value and ">" in value:
        return canonicalize_text(BeautifulSoup(value, "html.parser").get_text("\n", strip=True))
    return canonicalize_text(value)


def _ld_json_nodes(soup: BeautifulSoup) -> Iterable[Mapping[str, Any]]:
    for element in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(element.get_text())
        except (TypeError, ValueError):
            continue
        stack: list[Any] = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, Mapping):
                continue
            yield item
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)


def _is_article_node(node: Mapping[str, Any]) -> bool:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return any(isinstance(item, str) and item in ARTICLE_LD_TYPES for item in values)


def _ld_json_article(soup: BeautifulSoup) -> tuple[str, str, str]:
    """Return (articleBody, description, headline) of the first schema.org article."""
    for node in _ld_json_nodes(soup):
        if not _is_article_node(node):
            continue
        body = node.get("articleBody")
        description = node.get("description")
        headline = node.get("headline")
        return (
            _text_from_markup(body) if isinstance(body, str) else "",
            _text_from_markup(description) if isinstance(description, str) else "",
            canonicalize_text(headline)[:1000] if isinstance(headline, str) else "",
        )
    return "", "", ""


def _rich_text(node: Any, depth: int = 0) -> str:
    if depth > MAX_RICH_TEXT_DEPTH:
        return ""
    if isinstance(node, list):
        return "".join(_rich_text(item, depth + 1) for item in node)
    if not isinstance(node, Mapping):
        return ""
    value = node.get("value")
    if isinstance(value, str):
        return value
    content = node.get("content")
    if not isinstance(content, list):
        return ""
    inner = "".join(_rich_text(item, depth + 1) for item in content)
    if node.get("nodeType") in INLINE_RICH_TEXT_NODES:
        return inner
    return inner + "\n\n"


def _find_rich_text_root(value: Any, depth: int = 0) -> Mapping[str, Any] | None:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        if value.get("nodeType") == "document" and isinstance(value.get("content"), list):
            return value
        for item in value.values():
            found = _find_rich_text_root(item, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for item in value[:64]:
            found = _find_rich_text_root(item, depth + 1)
            if found is not None:
                return found
    return None


def _next_data_article(soup: BeautifulSoup) -> tuple[str, str]:
    element = soup.select_one("script#__NEXT_DATA__")
    if element is None:
        return "", ""
    try:
        value = json.loads(element.get_text())
    except (TypeError, ValueError):
        return "", ""
    props = value.get("props") if isinstance(value, Mapping) else None
    page_props = props.get("pageProps") if isinstance(props, Mapping) else None
    if not isinstance(page_props, Mapping):
        return "", ""
    root = _find_rich_text_root(page_props)
    if root is None:
        return "", ""
    title = ""
    for key in ("post", "article", "entry"):
        item = page_props.get(key)
        if isinstance(item, Mapping) and isinstance(item.get("title"), str):
            title = canonicalize_text(item["title"])[:1000]
            break
    return canonicalize_text(_rich_text(root)), title


def _structured_article(
    soup: BeautifulSoup, *, current_text: str, min_text_chars: int
) -> tuple[str, str, str]:
    """Recover article text that a site only publishes inside embedded structured data.

    schema.org ``articleBody`` and Contentful rich text are article bodies by
    definition, so they win whenever they are longer. ``description`` is only a
    body on sites that misuse it, so it is accepted solely as a last resort when
    ordinary extraction produced nothing usable.
    """
    article_body, description, headline = _ld_json_article(soup)
    if len(article_body) > len(current_text):
        return article_body, headline, "json_ld_article"
    next_text, next_title = _next_data_article(soup)
    if len(next_text) > len(current_text):
        return next_text, next_title, "next_data_article"
    if len(current_text) < min_text_chars <= len(description):
        return description, headline, "json_ld_description"
    return "", "", ""


def _visible_fallback(soup: BeautifulSoup) -> str:
    """Strip page chrome and return visible text. Mutates ``soup``, so it must run last."""
    for element in soup.select("script,style,noscript,svg,nav,header,footer,form"):
        element.decompose()
    root = soup.select_one("article") or soup.select_one("main") or soup.body
    if root is None:
        return ""
    return canonicalize_text(root.get_text("\n", strip=True))


def _json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        cleaned = canonicalize_text(value)
        if cleaned:
            yield cleaned
        return
    if isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"id", "name", "kind", "url", "permalink"}:
                continue
            yield from _json_strings(item)


def _reddit_comments(children: object) -> Iterable[str]:
    if not isinstance(children, list):
        return
    for child in children:
        if not isinstance(child, dict):
            continue
        data = child.get("data")
        if not isinstance(data, dict):
            continue
        body = data.get("body")
        if isinstance(body, str) and body not in {"[deleted]", "[removed]"}:
            yield body
        replies = data.get("replies")
        if isinstance(replies, dict):
            reply_data = replies.get("data")
            if isinstance(reply_data, dict):
                yield from _reddit_comments(reply_data.get("children"))


def _parse_reddit_json(value: Any) -> tuple[str, str]:
    if not isinstance(value, list) or not value:
        return "", ""
    title = ""
    parts: list[str] = []
    first = value[0]
    if isinstance(first, dict):
        listing = first.get("data")
        if isinstance(listing, dict):
            children = listing.get("children")
            if isinstance(children, list) and children:
                child = children[0]
                if isinstance(child, dict) and isinstance(child.get("data"), dict):
                    post = child["data"]
                    raw_title = post.get("title")
                    if isinstance(raw_title, str):
                        title = canonicalize_text(raw_title)
                        parts.append(title)
                    selftext = post.get("selftext")
                    if isinstance(selftext, str) and selftext not in {"[deleted]", "[removed]"}:
                        parts.append(selftext)
    if len(value) > 1 and isinstance(value[1], dict):
        comment_data = value[1].get("data")
        if isinstance(comment_data, dict):
            parts.extend(_reddit_comments(comment_data.get("children")))
    return canonicalize_text("\n\n".join(parts)), title[:1000]


def _docx_text(body: bytes) -> tuple[str, str]:
    """Read the paragraph text of a DOCX. Returns ``(text, title)``.

    Deliberately minimal, and deliberately not Docling (plan §11.1): a DOCX is a
    zip holding ``word/document.xml``, and the paragraph text is what evidence
    needs. Layout, tables, footnotes and numbering are not reconstructed - a
    quotation is a run of characters, and this produces exactly the run the
    document contains. It is what makes the canon sources that are held only as
    Word originals readable, with no new dependency.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entry = archive.getinfo(DOCX_BODY_ENTRY)
            if entry.file_size > MAX_DOCX_XML_BYTES:
                raise ValueError(f"docx body is {entry.file_size} bytes, over the limit")
            document = archive.read(DOCX_BODY_ENTRY)
            title = _docx_title(archive)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"not a readable docx: {exc}") from exc
    return canonicalize_text(_docx_paragraphs(document)), title


def _docx_title(archive: zipfile.ZipFile) -> str:
    try:
        properties = archive.read("docProps/core.xml")
    except KeyError:
        return ""
    for element in _parse_office_xml(properties).iter():
        if element.tag.endswith("}title") and element.text:
            return canonicalize_text(element.text)[:1000]
    return ""


def _docx_paragraphs(document: bytes) -> str:
    text_tag = f"{{{DOCX_NAMESPACE}}}t"
    spacing_tags = {f"{{{DOCX_NAMESPACE}}}tab", f"{{{DOCX_NAMESPACE}}}br"}
    lines: list[str] = []
    for paragraph in _parse_office_xml(document).iter(f"{{{DOCX_NAMESPACE}}}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == text_tag and node.text:
                pieces.append(node.text)
            elif node.tag in spacing_tags:
                pieces.append(" ")
        line = "".join(pieces).strip()
        if line:
            lines.append(line)
    return "\n\n".join(lines)


def _parse_office_xml(payload: bytes) -> Element:
    """Parse Office XML, refusing anything that declares entities.

    ``ElementTree`` does not resolve external entities, and refusing a DOCTYPE
    outright removes internal-entity expansion as well, so a file of unknown
    origin cannot turn a parse into a denial of service.
    """
    if b"<!DOCTYPE" in payload[:4096] or b"<!ENTITY" in payload[:4096]:
        raise ValueError("office xml declares a doctype or entity")
    try:
        return ElementTree.fromstring(payload)  # noqa: S314 - doctype refused above
    except ElementTree.ParseError as exc:
        raise ValueError(f"office xml is not well formed: {exc}") from exc


def parse_content(
    *,
    body: bytes,
    content_type: str,
    source_url: str,
    min_text_chars: int,
) -> ParsedContent:
    lowered_type = content_type.lower()
    title = ""
    quality = "unsupported"
    text = ""
    complete = False
    completion_threshold = min_text_chars

    if "pdf" in lowered_type or urlsplit(source_url).path.lower().endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(body))
            text = canonicalize_text(
                "\n\n".join(page.extract_text() or "" for page in reader.pages[:500])
            )
            metadata = reader.metadata
            title = canonicalize_text(str(metadata.title or ""))[:1000] if metadata else ""
            quality = "pdf_text"
            complete = len(reader.pages) <= 500 and len(text) >= min_text_chars
        except (OSError, PdfReadError, ValueError):
            # Historical PDFs may be truncated or structurally corrupt. The caller
            # still retains their raw entity body, and one bad document must not
            # abort the production batch.
            quality = "pdf_parse_error"
    elif lowered_type.startswith(DOCX_CONTENT_TYPE) or source_url.lower().endswith(".docx"):
        try:
            text, title = _docx_text(body)
            quality = "docx_text"
            complete = len(text) >= min_text_chars
        except ValueError:
            # A corrupt or password-protected Word file must not abort a batch; the
            # raw bytes stay retained and the failure is visible in the quality.
            quality = "docx_parse_error"
    elif "json" in lowered_type or source_url.lower().endswith(".json"):
        value = json.loads(_decode_text(body, content_type))
        host = (urlsplit(source_url).hostname or "").lower()
        if host.endswith("reddit.com"):
            text, title = _parse_reddit_json(value)
            quality = "reddit_json"
        else:
            text = canonicalize_text("\n".join(_json_strings(value)))
            quality = "json_text"
        complete = len(text) >= min_text_chars
    elif "html" in lowered_type or b"<html" in body[:4096].lower():
        html = _decode_text(body, content_type)
        soup = BeautifulSoup(html, "html.parser")
        title = _title_from_soup(soup)
        host = (urlsplit(source_url).hostname or "").lower()
        specialized_text = False
        if host in {"pandaily.com", "www.pandaily.com"}:
            text, remix_title = _remix_article(html)
            if text:
                title = remix_title or title
                quality = "remix_article"
                specialized_text = True
        if host in {"t.me", "telegram.me", "www.t.me"}:
            text = _telegram_text(soup)
            if text:
                quality = "telegram_html"
                specialized_text = True
                completion_threshold = min(min_text_chars, 20)
        if not specialized_text and len(text) < min_text_chars:
            # Imported at the point of use, not at module import. Reading the
            # store should not require an HTML extraction library: the embedder
            # runtime has torch and no lxml, and importing Database pulled in
            # trafilatura through the fetcher for no reason anybody wanted.
            import trafilatura

            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                favor_precision=False,
                favor_recall=True,
                output_format="txt",
                url=source_url,
            )
            text = canonicalize_text(extracted or "")
            quality = "trafilatura"
        if not specialized_text:
            structured_text, structured_title, structured_quality = _structured_article(
                soup, current_text=text, min_text_chars=min_text_chars
            )
            if structured_text:
                text = structured_text
                title = structured_title or title
                quality = structured_quality
        if not specialized_text and len(text) < min_text_chars:
            # Last, because it strips the script elements the structured pass reads.
            fallback = _visible_fallback(soup)
            if len(fallback) > len(text):
                text = fallback
                quality = "visible_fallback"
        lower_text = text.casefold()
        blocked = len(text) < 2000 and any(marker in lower_text for marker in BLOCK_PAGE_MARKERS)
        if blocked:
            quality = "blocked_page"
        complete = len(text) >= completion_threshold and not blocked
    elif lowered_type.startswith("text/"):
        text = canonicalize_text(_decode_text(body, content_type))
        quality = "plain_text"
        complete = len(text) >= min_text_chars

    return ParsedContent(
        text=text,
        title=title,
        language=_language(text),
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        quality=quality if text or quality.endswith("_error") else "no_text",
        is_complete=complete,
    )
