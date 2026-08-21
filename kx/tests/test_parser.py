from __future__ import annotations

import json

from radar_kx.parser import parse_content


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
