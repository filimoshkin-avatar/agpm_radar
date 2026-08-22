from __future__ import annotations

import io
import json
import zipfile

import pytest

from radar_kx.parser import (
    DOCX_CONTENT_TYPE,
    MAX_DOCX_XML_BYTES,
    _docx_text,
    parse_content,
)


def test_parses_article_html() -> None:
    body = b"""
    <html><head><title>Evidence title</title></head><body>
      <article><h1>Evidence title</h1><p>This is a sufficiently long factual paragraph about
      a production knowledge system. It contains enough words for extraction.</p></article>
    </body></html>
    """
    parsed = parse_content(
        body=body,
        content_type="text/html; charset=utf-8",
        source_url="https://example.com/article",
        min_text_chars=50,
    )
    assert parsed.is_complete
    assert "production knowledge system" in parsed.text
    assert parsed.title == "Evidence title"


def test_parses_telegram_message() -> None:
    body = """
    <html><head><title>Telegram</title></head><body>
      <div class="tgme_widget_message_author_name">Radar channel</div>
      <div class="tgme_widget_message_text">Очень длинный текст сообщения о внедрении
      искусственного интеллекта в управление проектами и проверяемых результатах.</div>
    </body></html>
    """.encode()
    parsed = parse_content(
        body=body,
        content_type="text/html; charset=utf-8",
        source_url="https://t.me/example/1",
        min_text_chars=50,
    )
    assert parsed.is_complete
    assert parsed.quality == "telegram_html"
    assert "искусственного интеллекта" in parsed.text


def test_short_telegram_message_is_complete_without_page_chrome() -> None:
    body = """
    <html><head><title>Telegram</title></head><body>
      <div class="tgme_widget_message_author_name">Radar channel</div>
      <div class="tgme_widget_message_text">Короткий, но полный факт о модели.</div>
      <div class="tgme_widget_message_views">3.15K views</div>
    </body></html>
    """.encode()
    parsed = parse_content(
        body=body,
        content_type="text/html; charset=utf-8",
        source_url="https://t.me/example/2?embed=1&mode=tme",
        min_text_chars=200,
    )
    assert parsed.is_complete
    assert parsed.quality == "telegram_html"
    assert "Короткий, но полный факт" in parsed.text
    assert "3.15K views" not in parsed.text


def test_parses_current_post_from_remix_stream_instead_of_featured_post() -> None:
    current = "<p>The current article contains the exact metric of 42 percent and its context.</p>"
    featured = "<p>" + "A much longer but unrelated featured article. " * 20 + "</p>"
    flat = [
        {"_1": 2},
        "loaderData",
        {"_3": 4},
        "routes/$slug",
        {"_5": 6, "_11": 12},
        "post",
        {"_7": 8, "_9": 10},
        "content",
        current,
        "title",
        "Current Radar evidence",
        "featuredPosts",
        [{"_7": 13}],
        featured,
    ]
    payload = json.dumps(json.dumps(flat, ensure_ascii=False))
    body = f"""
    <html><head><title>Shell title</title></head><body>
      <script>window.__remixContext.streamController.enqueue({payload})</script>
    </body></html>
    """.encode()
    parsed = parse_content(
        body=body,
        content_type="text/html; charset=utf-8",
        source_url="https://pandaily.com/current-radar-evidence",
        min_text_chars=40,
    )
    assert parsed.is_complete
    assert parsed.quality == "remix_article"
    assert parsed.title == "Current Radar evidence"
    assert "42 percent" in parsed.text
    assert "unrelated featured article" not in parsed.text


def test_parses_reddit_json_post_and_comments() -> None:
    body = json.dumps(
        [
            {"data": {"children": [{"data": {"title": "AI PMO", "selftext": "Body text"}}]}},
            {
                "data": {
                    "children": [
                        {"data": {"body": "First useful comment", "replies": ""}},
                        {"data": {"body": "[deleted]", "replies": ""}},
                    ]
                }
            },
        ]
    ).encode()
    parsed = parse_content(
        body=body,
        content_type="application/json",
        source_url="https://www.reddit.com/r/pmo/comments/abc/post.json",
        min_text_chars=20,
    )
    assert parsed.is_complete
    assert parsed.title == "AI PMO"
    assert "First useful comment" in parsed.text
    assert "[deleted]" not in parsed.text


def test_recovers_article_body_from_schema_org_json_ld() -> None:
    article_body = "".join(
        f"<p>Paragraph {index} of the full article states a verifiable metric of "
        "42 percent with its full context.</p>"
        for index in range(9)
    )
    ld_json = json.dumps(
        {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "WebSite", "name": "Example"},
                {
                    "@type": ["NewsArticle"],
                    "headline": "Structured evidence title",
                    "description": "Short teaser that must not become the body.",
                    "articleBody": article_body,
                },
            ],
        }
    )
    # Only the opening teaser is rendered, so visible extraction captures a fraction of
    # the evidence while the publisher ships the whole body in its structured data.
    body = f"""
    <html><head><title>Shell title</title>
    <script type="application/ld+json">{ld_json}</script></head>
    <body><main><article><h1>Structured evidence title</h1>
    <p>Only the opening teaser of this article is rendered in the served HTML, which is
    why an extractor that reads the visible page alone captures a fraction of it.</p>
    </article></main></body></html>
    """.encode()
    parsed = parse_content(
        body=body,
        content_type="text/html; charset=utf-8",
        source_url="https://example.com/structured",
        min_text_chars=60,
    )
    assert parsed.is_complete
    assert parsed.quality == "json_ld_article"
    assert parsed.title == "Structured evidence title"
    assert "Paragraph 8 of the full article" in parsed.text
    assert "must not become the body" not in parsed.text
    assert "<p>" not in parsed.text


def test_recovers_rich_text_article_from_next_data() -> None:
    payload = json.dumps(
        {
            "props": {
                "pageProps": {
                    "post": {
                        "title": "Next evidence title",
                        "content": {
                            "json": {
                                "nodeType": "document",
                                "content": [
                                    {
                                        "nodeType": "paragraph",
                                        "content": [
                                            {"nodeType": "text", "value": "First paragraph "},
                                            {
                                                "nodeType": "hyperlink",
                                                "content": [
                                                    {"nodeType": "text", "value": "with a link"}
                                                ],
                                            },
                                            {"nodeType": "text", "value": " inside it."},
                                        ],
                                    },
                                    {
                                        "nodeType": "paragraph",
                                        "content": [
                                            {
                                                "nodeType": "text",
                                                "value": "Second paragraph carries the "
                                                "verifiable metric of 17 percent.",
                                            }
                                        ],
                                    },
                                ],
                            }
                        },
                    }
                }
            }
        }
    )
    body = f"""
    <html><head><title>Shell title</title></head><body><main><p>Loading.</p></main>
    <script id="__NEXT_DATA__" type="application/json">{payload}</script></body></html>
    """.encode()
    parsed = parse_content(
        body=body,
        content_type="text/html; charset=utf-8",
        source_url="https://example.com/next",
        min_text_chars=60,
    )
    assert parsed.is_complete
    assert parsed.quality == "next_data_article"
    assert parsed.title == "Next evidence title"
    assert "First paragraph with a link inside it." in parsed.text
    assert "17 percent" in parsed.text


def test_structured_description_never_outranks_a_longer_extracted_body() -> None:
    ld_json = json.dumps(
        {
            "@type": "Article",
            "headline": "Structured headline",
            "description": "A teaser sentence.",
        }
    )
    body = f"""
    <html><head><title>Real title</title>
    <script type="application/ld+json">{ld_json}</script></head>
    <body><article><h1>Real title</h1><p>The visible article body is long enough to be
    accepted on its own and must not be replaced by a much shorter teaser.</p></article>
    </body></html>
    """.encode()
    parsed = parse_content(
        body=body,
        content_type="text/html; charset=utf-8",
        source_url="https://example.com/teaser",
        min_text_chars=60,
    )
    assert parsed.is_complete
    assert "visible article body" in parsed.text
    assert "A teaser sentence." not in parsed.text


def test_marks_visible_block_page_incomplete() -> None:
    body = b"""
    <html><body><main><h1>Attention Required</h1>
    <p>You have been blocked. Enable JavaScript and cookies to continue.</p>
    </main></body></html>
    """
    parsed = parse_content(
        body=body,
        content_type="text/html",
        source_url="https://example.com/blocked",
        min_text_chars=20,
    )
    assert not parsed.is_complete
    assert parsed.quality == "blocked_page"


def test_corrupt_pdf_is_isolated() -> None:
    parsed = parse_content(
        body=b"%PDF-1.7\ntruncated and corrupt",
        content_type="application/pdf",
        source_url="https://example.com/broken.pdf",
        min_text_chars=10,
    )
    assert parsed.text == ""
    assert parsed.quality == "pdf_parse_error"
    assert not parsed.is_complete


def _docx(document_xml: str, *, core_xml: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        if core_xml is not None:
            archive.writestr("docProps/core.xml", core_xml)
    return buffer.getvalue()


DOCX_BODY = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Агентное управление проектами</w:t></w:r></w:p>
    <w:p><w:r><w:t>First</w:t></w:r><w:tab/><w:r><w:t>second</w:t></w:r></w:p>
    <w:p/>
    <w:p><w:r><w:t xml:space="preserve">Split </w:t></w:r><w:r><w:t>run</w:t></w:r></w:p>
  </w:body>
</w:document>"""

DOCX_CORE = """<?xml version="1.0"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>AgPMBoK v0.7</dc:title>
</cp:coreProperties>"""


def test_docx_paragraphs_become_lines_and_runs_are_joined() -> None:
    parsed = parse_content(
        body=_docx(DOCX_BODY, core_xml=DOCX_CORE),
        content_type=DOCX_CONTENT_TYPE,
        source_url="agpm-canon:/originals/x.docx",
        min_text_chars=10,
    )
    assert parsed.quality == "docx_text"
    assert parsed.is_complete is True
    assert parsed.title == "AgPMBoK v0.7"
    # A paragraph is one line; a tab is a space; an empty paragraph is dropped; and
    # runs split mid-sentence by Word rejoin without a seam.
    assert parsed.text.split("\n\n") == [
        "Агентное управление проектами",
        "First second",
        "Split run",
    ]


def test_a_docx_without_core_properties_still_reads() -> None:
    parsed = parse_content(
        body=_docx(DOCX_BODY),
        content_type=DOCX_CONTENT_TYPE,
        source_url="agpm-canon:/originals/x.docx",
        min_text_chars=10,
    )
    assert parsed.quality == "docx_text"
    assert parsed.title == ""


def test_a_docx_is_recognised_by_extension_as_well_as_content_type() -> None:
    parsed = parse_content(
        body=_docx(DOCX_BODY),
        content_type="application/octet-stream",
        source_url="agpm-canon:/originals/x.DOCX",
        min_text_chars=10,
    )
    assert parsed.quality == "docx_text"


def test_a_short_docx_is_read_but_not_complete() -> None:
    short = DOCX_BODY.replace("Агентное управление проектами", "Hi")
    parsed = parse_content(
        body=_docx(short),
        content_type=DOCX_CONTENT_TYPE,
        source_url="agpm-canon:/originals/x.docx",
        min_text_chars=10_000,
    )
    assert parsed.quality == "docx_text"
    assert parsed.is_complete is False


def test_a_broken_docx_does_not_abort_the_batch() -> None:
    parsed = parse_content(
        body=b"PK\x03\x04 not really a zip",
        content_type=DOCX_CONTENT_TYPE,
        source_url="agpm-canon:/originals/x.docx",
        min_text_chars=10,
    )
    assert parsed.quality == "docx_parse_error"
    assert parsed.text == ""


def test_a_docx_missing_its_body_is_an_error_not_an_empty_document() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("docProps/core.xml", DOCX_CORE)
    parsed = parse_content(
        body=buffer.getvalue(),
        content_type=DOCX_CONTENT_TYPE,
        source_url="agpm-canon:/originals/x.docx",
        min_text_chars=10,
    )
    assert parsed.quality == "docx_parse_error"


def test_a_docx_that_declares_entities_is_refused() -> None:
    # The classic billion-laughs shape. ElementTree does not fetch external
    # entities, but an internal one can still expand, so a doctype is refused
    # outright rather than parsed and hoped about.
    bomb = (
        '<?xml version="1.0"?>\n<!DOCTYPE w:document [ <!ENTITY a "aaaaaaaaaa"> ]>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p></w:body></w:document>"
    )
    parsed = parse_content(
        body=_docx(bomb),
        content_type=DOCX_CONTENT_TYPE,
        source_url="agpm-canon:/originals/x.docx",
        min_text_chars=1,
    )
    assert parsed.quality == "docx_parse_error"


def test_an_oversized_docx_body_is_refused_before_it_is_read() -> None:
    with pytest.raises(ValueError, match="over the limit"):
        _docx_text(_oversized_docx())


def _oversized_docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # Highly compressible, so the archive stays small while the entry declares
        # a size past the cap - exactly the shape the cap exists for.
        archive.writestr("word/document.xml", "<a/>" + " " * (MAX_DOCX_XML_BYTES + 1))
    return buffer.getvalue()
