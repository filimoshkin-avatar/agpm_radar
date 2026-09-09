"""Shared, dependency-free material title gate (Legacy and V2).

Trust order: Article JSON-LD headline, Open Graph, Twitter, Article JSON-LD
name, h1, document title. Repeated normalized values count once per source.
Within a trust tier independent agreement wins; unresolved ties fail closed.
h1/document title alone cannot authorize a repair. Site suffixes are removed
only when named by og:site_name or corroborated by another complete candidate.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import contextlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

_ROLES = frozenset(
    [
        "user",
        "assistant",
        "system",
        "developer",
        "human",
        "tool",
        "function",
        "model",
        "bot",
        "chatbot",
        "пользователь",
        "ассистент",
        "система",
        "разработчик",
        "помощник",
    ]
)
_PLACEHOLDERS = frozenset(
    {
        "",
        "null",
        "none",
        "undefined",
        "unknown",
        "untitled",
        "no title",
        "title",
        "headline",
        "n a",
        "na",
        "tbd",
        "todo",
        "placeholder",
        "test",
        "error",
        "not found",
        "404",
        "403",
        "access denied",
        "forbidden",
        "just a moment",
        "please wait",
        "loading",
        "enable javascript",
        "verify you are human",
        "attention required",
        "без названия",
        "заголовок",
        "нет данных",
        "ошибка",
        "страница не найдена",
    }
)
_ARTICLE_TYPES = frozenset(
    {
        "Article",
        "NewsArticle",
        "BlogPosting",
        "Report",
        "ScholarlyArticle",
        "TechArticle",
        "AnalysisNewsArticle",
        "PressRelease",
    }
)


def normalize_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", html.unescape(value)).split())


def _key(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", normalize_title(value).casefold()))


def title_problem(value: object, reliable_title: str | None = None) -> str | None:
    """Short meaningful titles are valid; there is no minimum token count."""
    if not isinstance(value, str) or not normalize_title(value):
        return "empty_title"
    normalized = normalize_title(value)
    key = _key(value)
    tokens = key.split()
    # ChatML and common role/message wrappers, without rejecting real articles
    # such as 'User experience' or 'System design'.
    wrapper = [t for t in tokens if t not in {"role", "message", "im", "start", "end"}]
    if key in _ROLES or (wrapper and all(t in _ROLES for t in wrapper)):
        return "dialogue_role"
    error_key = " ".join(token for token in tokens if not re.fullmatch(r"[45]\d\d", token))
    if (
        key in _PLACEHOLDERS
        or error_key in _PLACEHOLDERS
        or error_key
        in {
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "internal server error",
            "please enable javascript",
            "please enable javascript and cookies to continue",
            "enable javascript and cookies to continue",
            "checking your browser",
            "security verification",
            "robot check",
            "verify you are a human",
        }
    ):
        return "technical_placeholder"
    if re.fullmatch(r"(?i)(?:https?://|www\.)\S+", normalized) or re.fullmatch(
        r"[\w.-]+\.[a-zA-Z]{2,}(?:/\S*)?", normalized
    ):
        return "url_as_title"
    if not any(c.isalpha() for c in key) or len(key) == 1 or re.fullmatch(r"(.)\1{2,}", key):
        return "meaningless_title"
    if reliable_title is not None:
        left, right = set(tokens), set(_key(reliable_title).split())
        if left != right and len(left & right) / max(len(left), len(right), 1) < 0.45:
            return "page_title_mismatch"
    return None


def title_diagnostic(value: object, url: object, reason: str) -> str:
    # JSON escaping prevents control characters/newlines from forging log lines.
    return (
        f"TITLE_QUALITY_GATE reason={reason} url="
        f"{json.dumps(str(url)[:1000], ensure_ascii=True)} value="
        f"{json.dumps(str(value)[:160], ensure_ascii=True)}"
    )


class TitleQualityError(ValueError):
    """A suspect title has no sufficiently reliable source-page replacement."""


def require_title(value: object, url: object = "") -> None:
    if reason := title_problem(value):
        raise TitleQualityError(title_diagnostic(value, url, reason))


@dataclass(frozen=True)
class TitleCandidate:
    title: str
    source: str
    priority: int


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[TitleCandidate] = []
        self.site_names: list[str] = []
        self.capture: str | None = None
        self.parts: list[str] = []

    def add(self, value: object, source: str, priority: int) -> None:
        if isinstance(value, str):
            self.candidates.append(TitleCandidate(normalize_title(value), source, priority))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta":
            name = (attributes.get("property") or attributes.get("name") or "").lower()
            if name == "og:site_name" and attributes.get("content"):
                self.site_names.append(str(attributes["content"]))
            if name in {"og:title", "twitter:title"}:
                self.add(attributes.get("content"), name, 1 if name == "og:title" else 2)
        if tag in {"h1", "title"} or (
            tag == "script" and (attributes.get("type") or "").lower() == "application/ld+json"
        ):
            self.capture, self.parts = tag, []

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != self.capture:
            return
        value = " ".join(self.parts)
        self.capture = None
        if tag == "script":
            with contextlib.suppress(ValueError, RecursionError):
                self.read_jsonld(json.loads(value))
        else:
            self.add(value, tag, 4 if tag == "h1" else 5)

    def read_jsonld(self, node: object) -> None:
        if isinstance(node, list):
            for child in node:
                self.read_jsonld(child)
        elif isinstance(node, dict):
            types = node.get("@type", [])
            if isinstance(types, str):
                types = [types.rsplit("/", 1)[-1]]
            if isinstance(types, list) and any(
                t in _ARTICLE_TYPES for t in types if isinstance(t, str)
            ):
                self.add(node.get("headline"), "jsonld:headline", 0)
                self.add(node.get("name"), "jsonld:name", 3)
            for child in node.values():
                if isinstance(child, dict | list):
                    self.read_jsonld(child)


def extract_title_candidates(markup: str) -> list[TitleCandidate]:
    parser = _TitleParser()
    parser.feed(markup[:1_000_000])
    keys = {_key(c.title) for c in parser.candidates}
    sites = {_key(s) for s in parser.site_names}
    result: list[TitleCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in parser.candidates:
        title = candidate.title
        separators = list(re.finditer(r"\s+[|\-–—»]\s+", title))
        if separators:
            last = separators[-1]
            prefix, suffix = title[: last.start()], title[last.end() :]
            if _key(suffix) in sites or _key(prefix) in keys:
                title = prefix
        identity = (_key(title), candidate.source)
        if identity not in seen and title_problem(title) is None:
            result.append(TitleCandidate(title, candidate.source, candidate.priority))
            seen.add(identity)
    return result


def reliable_page_title(markup: str) -> TitleCandidate | None:
    candidates = extract_title_candidates(markup)
    groups: dict[str, list[TitleCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(_key(candidate.title), []).append(candidate)
    ranked: list[tuple[int, int, str, TitleCandidate]] = []
    for key, group in groups.items():
        best = min(group, key=lambda c: (c.priority, c.title))
        support = len({c.source for c in group})
        if best.priority < 4 or support >= 2:
            ranked.append((best.priority, -support, key, best))
    ranked.sort(key=lambda row: row[:3])
    if not ranked or (len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]):
        return None
    return ranked[0][3]


@dataclass(frozen=True)
class TitleResolution:
    title: str
    reason: str | None = None
    source: str | None = None


def resolve_title(value: object, markup: str, url: str = "") -> TitleResolution:
    candidate = reliable_page_title(markup)
    return resolve_title_candidate(value, candidate, url)


def resolve_title_candidate(
    value: object, candidate: TitleCandidate | None, url: str = ""
) -> TitleResolution:
    reason = title_problem(value, candidate.title if candidate else None)
    if reason:
        if candidate is None:
            raise TitleQualityError(
                title_diagnostic(value, url, reason + ":no_reliable_html_title")
            )
        return TitleResolution(candidate.title, reason, candidate.source)
    return TitleResolution(str(value))


def title_reference_problem(value: object) -> str | None:
    """Detect explicit article references to rejected titles in derived prose."""
    if isinstance(value, str):
        for match in re.finditer(
            r"""(?i)(?:материал\w*|стать\w*|article|titled)\s+[«„“"']([^»“”"']{1,160})[»“”"']""",
            value,
        ):
            if title_problem(match[1]):
                return "stale_title_reference"
    elif isinstance(value, dict):
        for child in value.values():
            if problem := title_reference_problem(child):
                return problem
    elif isinstance(value, list):
        for child in value:
            if problem := title_reference_problem(child):
                return problem
    return None


def repair_title_reference(text: str, old: str, new: str) -> str:
    """Replace explicit quoted/exact references, never ordinary uses of 'user'."""
    if not old:
        return text
    if normalize_title(text) == normalize_title(old):
        return new
    for opening, closing in (("«", "»"), ("„", "“"), ("“", "”"), ('"', '"'), ("'", "'")):
        replacement_text = opening + new + closing

        def replacement(_: re.Match[str], value: str = replacement_text) -> str:
            return value

        text = re.sub(
            re.escape(opening) + r"\s*" + re.escape(old.strip()) + r"\s*" + re.escape(closing),
            replacement,
            text,
            flags=re.IGNORECASE,
        )
    prefix = "Материал требует ручного просмотра: "
    if text == prefix + old:
        return prefix + new
    return text
