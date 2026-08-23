"""Enough markdown to read a slice document in a browser.

Not a markdown implementation. It renders the subset the project's own documents
use - headings, paragraphs, lists, tables, fenced code, block quotes, bold, inline
code and links - and nothing else.

Everything is escaped first and only this module's own tags are emitted, so a
document can never inject markup into the page that shows it. That matters less
for documents we wrote than it would for somebody else's, and it costs one
function either way.

A dependency would do more of markdown. It would also be carried by every
deployment of the worker for the sake of a reading pane, and the locked
requirements are a gate this project pays attention to.
"""

from __future__ import annotations

import html
import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_ITEM = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BARE_URL = re.compile(r"(?<![\">])(https?://[^\s<>\")]+)")


def _inline(text: str) -> str:
    """Escape first, then add only our own tags."""
    escaped = html.escape(text, quote=False)
    escaped = _CODE.sub(lambda match: f"<code>{match.group(1)}</code>", escaped)
    escaped = _BOLD.sub(lambda match: f"<strong>{match.group(1)}</strong>", escaped)
    escaped = _LINK.sub(
        lambda match: (
            f'<a href="{html.escape(match.group(2), quote=True)}"'
            f' target="_blank" rel="noreferrer noopener">{match.group(1)}</a>'
        ),
        escaped,
    )
    escaped = _BARE_URL.sub(
        lambda match: (
            f'<a href="{html.escape(match.group(1), quote=True)}"'
            f' target="_blank" rel="noreferrer noopener">{match.group(1)}</a>'
        ),
        escaped,
    )
    return escaped


def _table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render(markdown: str) -> str:
    """Render the subset. Anything unrecognised becomes a paragraph."""
    lines = markdown.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]

        if line.strip().startswith("```"):
            fence: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                fence.append(lines[index])
                index += 1
            index += 1
            out.append("<pre><code>" + html.escape("\n".join(fence)) + "</code></pre>")
            continue

        heading = _HEADING.match(line)
        if heading:
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        # A table is a header row, a divider, then rows until a blank line.
        if "|" in line and index + 1 < len(lines) and _TABLE_DIVIDER.match(lines[index + 1]):
            header = _table_row(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_row(lines[index]))
                index += 1
            head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue

        if _LIST_ITEM.match(line) or _ORDERED_ITEM.match(line):
            ordered = _ORDERED_ITEM.match(line) is not None
            items: list[str] = []
            while index < len(lines):
                match = (
                    _ORDERED_ITEM.match(lines[index]) if ordered else _LIST_ITEM.match(lines[index])
                )
                if match is None:
                    break
                items.append(f"<li>{_inline(match.group(1))}</li>")
                index += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue

        if _QUOTE.match(line):
            quoted: list[str] = []
            while index < len(lines):
                match = _QUOTE.match(lines[index])
                if match is None:
                    break
                quoted.append(match.group(1))
                index += 1
            out.append("<blockquote>" + _inline(" ".join(quoted)) + "</blockquote>")
            continue

        if not line.strip():
            index += 1
            continue

        paragraph: list[str] = []
        while index < len(lines) and lines[index].strip() and not _HEADING.match(lines[index]):
            if lines[index].strip().startswith("```") or _LIST_ITEM.match(lines[index]):
                break
            paragraph.append(lines[index].strip())
            index += 1
        if paragraph:
            out.append("<p>" + _inline(" ".join(paragraph)) + "</p>")

    return "\n".join(out)
