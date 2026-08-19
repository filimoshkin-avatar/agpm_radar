#!/usr/bin/env python3
"""Collect incremental materials for the weekly AgPM activity radar."""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.parse
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "knowledge/agpm-radar/sources.yml"
DEFAULT_WIKI = ROOT / "knowledge/agpm-radar"


TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
}

PERIMETER_KEYWORDS = {
    "near": [
        "agpm",
        "agentic project management",
        "ai pmo",
        "project management",
        "portfolio management",
        "project portfolio",
        "pmo",
        "проектн",
        "портфел",
        "проектный офис",
        "управлени проект",
    ],
    "middle": [
        "business process",
        "workflow",
        "enterprise",
        "operating model",
        "business operations",
        "bpm",
        "case study",
        "внедрен",
        "бизнес-процесс",
        "бизнес процесс",
        "операционн",
        "корпоративн",
    ],
    "far": [
        "ai agent",
        "ai agents",
        "agentic ai",
        "computer use",
        "autonomous agent",
        "business agent",
        "perplexity computer",
        "openclaw",
        "agent workspace",
        "агент",
        "ии-агент",
    ],
}

LOW_YIELD_EXPANSION_QUERIES = {
    "far": [
        '(AI agents OR agentic AI) (governance OR audit OR compliance OR risk OR "identity management" OR "non-human identity") enterprise news',
        '(AI agents OR agentic AI) ("agent economy" OR infrastructure OR funding OR platform OR "business agents") enterprise latest',
        '(AI agents OR agentic AI) (OpenAI OR Anthropic OR Microsoft OR Google OR Meta OR Salesforce OR ServiceNow OR Workday) enterprise workflow',
    ],
    "middle": [
        '(agentic AI OR AI agents) ("business process" OR workflow OR operations OR finance OR procurement OR customer support) case study',
        '(AI agents OR agentic automation) ("operating model" OR orchestration OR "human oversight" OR "process governance") enterprise',
        '(agentic AI OR AI agents) (BPM OR "business process management" OR "enterprise automation") research report',
    ],
    "near": [
        '("AI PMO" OR "agentic project management" OR "AI agents for project management" OR "project management agents")',
        '("AI agent" "project portfolio" OR "AI agent" PMO OR "agentic PMO" OR "portfolio management" "AI agents")',
        '("project management" "AI agents" "human oversight" OR "PMO" "agentic AI" governance OR "project controls" "AI agent")',
    ],
}


@dataclass
class Candidate:
    title: str
    url: str
    source_id: str
    source_title: str
    source_url: str
    provider: str
    published_at: str | None = None
    summary: str | None = None
    raw_excerpt: str | None = None
    query: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_dt(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    relative = re.fullmatch(r"(\d+)\s+(day|days|week|weeks|month|months|year|years)\s+ago", value.lower())
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        days = amount
        if unit.startswith("week"):
            days = amount * 7
        elif unit.startswith("month"):
            days = amount * 30
        elif unit.startswith("year"):
            days = amount * 365
        return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        pass
    try:
        fixed = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(fixed)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        pass
    for fmt in ("%A, %B %d, %Y", "%a, %b %d, %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return value


def parse_web_research_published_at(value: str | None) -> str | None:
    if not value:
        return None
    raw = strip_text(str(value), 120)
    if not raw:
        return None
    patterns = [
        r"\d{4}-\d{2}-\d{2}(?:[T\s].*)?",
        r"\d+\s+(?:day|days|week|weeks|month|months|year|years)\s+ago",
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{4}.*",
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}",
        r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4}",
    ]
    if not any(re.fullmatch(pattern, raw, flags=re.IGNORECASE) for pattern in patterns):
        return None
    parsed = parse_dt(raw)
    if not parsed:
        return None
    try:
        datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    except Exception:
        return None
    return parsed


def parse_dt_obj(value: str | None) -> datetime | None:
    parsed = parse_dt(value)
    if not parsed:
        return None
    try:
        return datetime.fromisoformat(parsed.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def lookback_cutoff(config: dict[str, Any], now: datetime | None = None) -> datetime | None:
    days = config.get("collection", {}).get("lookback_days")
    if not days:
        return None
    base = now or datetime.now(timezone.utc)
    return base - timedelta(days=int(days))


def filter_by_lookback(candidates: list[Candidate], config: dict[str, Any]) -> list[Candidate]:
    cutoff = lookback_cutoff(config)
    if not cutoff:
        return candidates
    filtered: list[Candidate] = []
    for candidate in candidates:
        published = parse_dt_obj(candidate.published_at)
        if published is None or published >= cutoff:
            filtered.append(candidate)
    return filtered


def strip_text(value: str | None, limit: int = 900) -> str:
    if not value:
        return ""
    value = html.unescape(str(value))
    if "<" in value and ">" in value:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            text = BeautifulSoup(value, "html.parser").get_text(" ")
    else:
        text = value
    text = re.sub(r"<<<EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>", " ", text)
    text = re.sub(r"<<<END_EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>", " ", text)
    text = re.sub(r"\bSource:\s*Web Search\s*---", " ", text)
    text = re.sub(r"<<\s*>>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def normalize_title(value: str) -> str:
    value = strip_text(value, 300).lower()
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    url = urllib.parse.urljoin("https://example.invalid", url) if url.startswith("/") else url
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    query = [(k, v) for k, v in query if k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    clean_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunsplit((scheme, netloc, path, clean_query, ""))


def material_id(title: str, url: str, published_at: str | None = None) -> str:
    canonical = canonicalize_url(url)
    if canonical and not canonical.startswith("https://example.invalid"):
        basis = canonical
    else:
        basis = "|".join([normalize_title(title), published_at or ""])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def classify_perimeter(title: str, summary: str | None, source_title: str) -> str:
    haystack = " ".join([title or "", summary or "", source_title or ""]).lower()
    scores = {key: 0 for key in PERIMETER_KEYWORDS}
    for perimeter, words in PERIMETER_KEYWORDS.items():
        for word in words:
            if word in haystack:
                scores[perimeter] += 1
    if scores["near"]:
        return "near"
    if "web research: far" in haystack:
        return "far"
    if "web research: middle" in haystack:
        return "middle"
    if scores["middle"]:
        return "middle"
    if scores["far"]:
        return "far"
    return "watch"


def make_summary(title: str, raw: str | None) -> str:
    raw = strip_text(raw or "", 700)
    if raw:
        return raw
    return f"Материал требует ручного просмотра: {title}"


def request_get(url: str, user_agent: str, timeout: int = 20) -> requests.Response:
    headers = {"User-Agent": user_agent, "Accept-Language": "ru,en;q=0.8"}
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response


def source_limit(config: dict[str, Any]) -> int:
    return int(config.get("collection", {}).get("max_items_per_source", 50))


def collect_telegram(source: dict[str, Any], config: dict[str, Any]) -> list[Candidate]:
    channel = source.get("channel") or source["url"].rstrip("/").split("/")[-1]
    url = f"https://t.me/s/{channel}"
    user_agent = config["collection"]["user_agent"]
    soup = BeautifulSoup(request_get(url, user_agent).text, "html.parser")
    items: list[Candidate] = []
    for message in soup.select(".tgme_widget_message")[: source_limit(config)]:
        date_link = message.select_one("a.tgme_widget_message_date")
        text_node = message.select_one(".tgme_widget_message_text")
        title = strip_text(text_node.get_text(" ", strip=True) if text_node else "", 180)
        if not title:
            title = f"{source['title']}: сообщение Telegram"
        link = date_link.get("href") if date_link else source["url"]
        time_node = message.select_one("time")
        published_at = parse_dt(time_node.get("datetime") if time_node else None)
        items.append(
            Candidate(
                title=title,
                url=link,
                source_id=source["id"],
                source_title=source["title"],
                source_url=source["url"],
                provider="telegram",
                published_at=published_at,
                summary=make_summary(title, text_node.decode_contents() if text_node else ""),
                raw_excerpt=strip_text(text_node.decode_contents() if text_node else "", 1000),
            )
        )
    return items


def habr_rss_url(source_url: str) -> str | None:
    match = re.search(r"/hubs/([^/]+)/articles", source_url)
    if not match:
        return None
    return f"https://habr.com/ru/rss/hub/{match.group(1)}/articles/?fl=ru"


def collect_rss(url: str, source: dict[str, Any], config: dict[str, Any], provider: str = "rss") -> list[Candidate]:
    user_agent = config["collection"]["user_agent"]
    root = ET.fromstring(request_get(url, user_agent).content)
    items: list[Candidate] = []
    for node in root.findall(".//item")[: source_limit(config)]:
        title = node.findtext("title") or source["title"]
        link = node.findtext("link") or source["url"]
        pub = parse_dt(node.findtext("pubDate") or node.findtext("date"))
        desc = node.findtext("description") or node.findtext("summary") or ""
        items.append(
            Candidate(
                title=strip_text(title, 220),
                url=link,
                source_id=source["id"],
                source_title=source["title"],
                source_url=source["url"],
                provider=provider,
                published_at=pub,
                summary=make_summary(title, desc),
                raw_excerpt=strip_text(desc, 1000),
            )
        )
    return items


def collect_atom(url: str, source: dict[str, Any], config: dict[str, Any], provider: str = "atom") -> list[Candidate]:
    user_agent = config["collection"]["user_agent"]
    root = ET.fromstring(request_get(url, user_agent).content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items: list[Candidate] = []
    for entry in root.findall("atom:entry", ns)[: source_limit(config)]:
        title = entry.findtext("atom:title", default=source["title"], namespaces=ns)
        summary = entry.findtext("atom:summary", default="", namespaces=ns) or entry.findtext("atom:content", default="", namespaces=ns)
        published_at = parse_dt(entry.findtext("atom:published", default="", namespaces=ns) or entry.findtext("atom:updated", default="", namespaces=ns))
        link = ""
        for link_node in entry.findall("atom:link", ns):
            if link_node.get("rel") == "alternate" or not link:
                link = link_node.get("href", "")
        items.append(
            Candidate(
                title=strip_text(title, 220),
                url=link or source["url"],
                source_id=source["id"],
                source_title=source["title"],
                source_url=source["url"],
                provider=provider,
                published_at=published_at,
                summary=make_summary(title, summary),
                raw_excerpt=strip_text(summary, 1000),
            )
        )
    return items


def collect_habr(source: dict[str, Any], config: dict[str, Any]) -> list[Candidate]:
    rss = habr_rss_url(source["url"])
    if rss:
        try:
            return collect_rss(rss, source, config, provider="habr_rss")
        except Exception:
            pass

    user_agent = config["collection"]["user_agent"]
    soup = BeautifulSoup(request_get(source["url"], user_agent).text, "html.parser")
    items: list[Candidate] = []
    for article in soup.select("article")[: source_limit(config)]:
        link_node = article.select_one("a.tm-title__link, h2 a, a[href*='/ru/articles/']")
        if not link_node:
            continue
        title = strip_text(link_node.get_text(" ", strip=True), 220)
        link = urllib.parse.urljoin("https://habr.com", link_node.get("href", ""))
        time_node = article.select_one("time")
        published_at = parse_dt(time_node.get("datetime") if time_node else None)
        body = article.select_one(".article-formatted-body, .tm-article-snippet, .tm-article-body")
        items.append(
            Candidate(
                title=title,
                url=link,
                source_id=source["id"],
                source_title=source["title"],
                source_url=source["url"],
                provider="habr_html",
                published_at=published_at,
                summary=make_summary(title, body.get_text(" ", strip=True) if body else ""),
                raw_excerpt=strip_text(body.get_text(" ", strip=True) if body else "", 1000),
            )
        )
    return items


def collect_reddit(source: dict[str, Any], config: dict[str, Any]) -> list[Candidate]:
    subreddit = source.get("subreddit") or source["url"].rstrip("/").split("/")[-1]
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={source_limit(config)}"
    try:
        data = request_get(url, config["collection"]["user_agent"]).json()
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 403:
            raise
        return collect_atom(f"https://www.reddit.com/r/{subreddit}/new/.rss", source, config, provider="reddit_atom")
    items: list[Candidate] = []
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {})
        title = post.get("title") or source["title"]
        permalink = urllib.parse.urljoin("https://www.reddit.com", post.get("permalink", ""))
        published_at = None
        if post.get("created_utc"):
            published_at = datetime.fromtimestamp(float(post["created_utc"]), timezone.utc).replace(microsecond=0).isoformat()
        summary = post.get("selftext") or post.get("url_overridden_by_dest") or ""
        items.append(
            Candidate(
                title=strip_text(title, 220),
                url=permalink,
                source_id=source["id"],
                source_title=source["title"],
                source_url=source["url"],
                provider="reddit_json",
                published_at=published_at,
                summary=make_summary(title, summary),
                raw_excerpt=strip_text(summary, 1000),
            )
        )
    return items


def arxiv_archive_categories(archive: str) -> list[str]:
    if archive == "econ":
        return ["econ.EM", "econ.GN", "econ.TH"]
    return [archive]


def collect_arxiv_search(source: dict[str, Any], config: dict[str, Any]) -> list[Candidate]:
    query = source.get("query") or urllib.parse.parse_qs(urllib.parse.urlsplit(source["url"]).query).get("query", [""])[0]
    archive = source.get("archive") or source["url"].rstrip("/").split("/")[-1]
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    term_query = "+AND+".join(f"all:{urllib.parse.quote_plus(term)}" for term in terms) or "all:project"
    categories = "+OR+".join(f"cat:{category}" for category in arxiv_archive_categories(archive))
    search_query = f"({term_query})+AND+({categories})"
    params = {
        "search_query": search_query,
        "start": "0",
        "max_results": str(source_limit(config)),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    api_url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params, safe="()+:")
    root = ET.fromstring(request_get(api_url, config["collection"]["user_agent"], timeout=30).content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items: list[Candidate] = []
    for entry in root.findall("atom:entry", ns):
        title = strip_text(entry.findtext("atom:title", default="", namespaces=ns), 220)
        summary = entry.findtext("atom:summary", default="", namespaces=ns)
        published_at = parse_dt(entry.findtext("atom:published", default="", namespaces=ns))
        link = ""
        for link_node in entry.findall("atom:link", ns):
            if link_node.get("rel") == "alternate" or not link:
                link = link_node.get("href", "")
        items.append(
            Candidate(
                title=title or source["title"],
                url=link or source["url"],
                source_id=source["id"],
                source_title=source["title"],
                source_url=source["url"],
                provider="arxiv_api",
                published_at=published_at,
                summary=make_summary(title, summary),
                raw_excerpt=strip_text(summary, 1000),
                query=query,
            )
        )
    return items


def collect_web_page(source: dict[str, Any], config: dict[str, Any]) -> list[Candidate]:
    soup = BeautifulSoup(request_get(source["url"], config["collection"]["user_agent"]).text, "html.parser")
    title = strip_text(soup.title.get_text(" ", strip=True) if soup.title else source["title"], 220)
    desc_node = soup.select_one("meta[name='description'], meta[property='og:description']")
    desc = desc_node.get("content") if desc_node else ""
    article_node = soup.select_one("article, main")
    article_text = strip_text(article_node.get_text(" ", strip=True) if article_node else "", 3000)
    summary_text = make_summary(title, " ".join(part for part in [desc, article_text] if part))
    return [
        Candidate(
            title=title,
            url=source["url"],
            source_id=source["id"],
            source_title=source["title"],
            source_url=source["url"],
            provider="web_html",
            published_at=None,
            summary=summary_text,
            raw_excerpt=strip_text(article_text or desc, 3000),
        )
    ]


def is_internal_aiagents_link(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    return host.endswith("aiagentsdirectory.com") or not host


def latest_aiagents_digest_url(source: dict[str, Any], config: dict[str, Any]) -> str:
    soup = BeautifulSoup(request_get(source["url"], config["collection"]["user_agent"]).text, "html.parser")
    for link in soup.select("h2 a[href], h3 a[href], a[href]"):
        href = link.get("href") or ""
        if not href.startswith("/news/") and not href.startswith("https://aiagentsdirectory.com/news/"):
            continue
        if "/news/topic/" in href or href.rstrip("/") == "/news":
            continue
        text = strip_text(link.get_text(" ", strip=True), 220).lower()
        if "news brief" in text or "daily" in text or "ai agents" in text:
            return urllib.parse.urljoin(source["url"], href)
    raise ValueError("latest daily digest link was not found")


def digest_context(article: Any) -> str:
    parts: list[str] = []
    for paragraph in article.select("p.text-sm.leading-relaxed.text-gray-700"):
        text = strip_text(paragraph.get_text(" ", strip=True), 700)
        if not text:
            continue
        if "Why it matters:" in text:
            continue
        parts.append(text)
        if len(parts) >= 2:
            break
    return " ".join(parts)


def published_from_text(text: str) -> str | None:
    match = re.search(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"\d{1,2},\s+\d{4}",
        text,
    )
    return parse_dt(match.group(0)) if match else None


def collect_ai_agents_directory_daily(source: dict[str, Any], config: dict[str, Any]) -> list[Candidate]:
    digest_url = latest_aiagents_digest_url(source, config)
    soup = BeautifulSoup(request_get(digest_url, config["collection"]["user_agent"]).text, "html.parser")
    article = soup.find("article")
    if article is None:
        raise ValueError("daily digest article was not found")

    digest_title_node = article.select_one("h1, h2, h3")
    digest_title = strip_text(digest_title_node.get_text(" ", strip=True) if digest_title_node else source["title"], 220)
    digest_text = strip_text(article.get_text(" ", strip=True), 4000)
    digest_date = published_from_text(digest_text)
    context = digest_context(article)
    items: list[Candidate] = []

    headline_blocks = []
    for block in article.select("div.space-y-1"):
        link = block.select_one("a[href]")
        if not link:
            continue
        url = urllib.parse.urljoin(digest_url, link.get("href") or "")
        if is_internal_aiagents_link(url):
            continue
        headline_blocks.append(block)

    for block in headline_blocks[: source_limit(config)]:
        link = block.select_one("a[href]")
        if not link:
            continue
        title = strip_text(link.get_text(" ", strip=True), 220)
        url = urllib.parse.urljoin(digest_url, link.get("href") or "")
        block_text = strip_text(block.get_text(" ", strip=True), 1200)
        published_at = published_from_text(block_text) or digest_date
        paragraphs = [strip_text(p.get_text(" ", strip=True), 700) for p in block.select("p")]
        paragraphs = [p for p in paragraphs if p and title not in p]
        summary = " ".join(paragraphs) or block_text
        if context:
            summary = f"{summary} Контекст daily digest: {context}"
        items.append(
            Candidate(
                title=title or digest_title,
                url=url,
                source_id=source["id"],
                source_title=f"{source['title']}: {digest_title}",
                source_url=digest_url,
                provider="ai_agents_directory_daily",
                published_at=published_at,
                summary=make_summary(title or digest_title, summary),
                raw_excerpt=strip_text(block_text, 1000),
                query=digest_title,
            )
        )
    return items


def env_first(names: Iterable[str]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def collect_brave(query: str, perimeter: str, config: dict[str, Any]) -> tuple[list[Candidate], str | None]:
    provider_config = config["web_research"]["providers"]["brave"]
    api_key = env_first(provider_config.get("env_keys", []))
    if not api_key:
        return [], "BRAVE_SEARCH_API_KEY/BRAVE_API_KEY is not set"
    params = {
        "q": query,
        "count": int(config["collection"].get("max_search_results_per_query", 10)),
        "search_lang": "en",
        "country": "US",
        "freshness": "pm" if int(config["collection"].get("lookback_days", 7)) > 7 else "pw",
    }
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
        "User-Agent": config["collection"]["user_agent"],
    }
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params=params,
        headers=headers,
        timeout=int(provider_config.get("timeout_seconds", 30)),
    )
    response.raise_for_status()
    data = response.json()
    items: list[Candidate] = []
    for row in data.get("web", {}).get("results", []):
        title = strip_text(row.get("title") or query, 220)
        items.append(
            Candidate(
                title=title,
                url=row.get("url") or "",
                source_id=f"web_brave_{perimeter}",
                source_title=f"Brave web research: {perimeter}",
                source_url="https://search.brave.com/",
                provider="brave",
                published_at=parse_web_research_published_at(row.get("age")),
                summary=make_summary(title, row.get("description") or ""),
                raw_excerpt=strip_text(row.get("description") or "", 1000),
                query=query,
            )
        )
    return items, None


def collect_perplexity(query: str, perimeter: str, config: dict[str, Any]) -> tuple[list[Candidate], str | None]:
    provider_config = config["web_research"]["providers"]["perplexity"]
    api_key = env_first(provider_config.get("env_keys", []))
    if not api_key:
        return [], "PERPLEXITY_API_KEY/PPLX_API_KEY is not set"
    freshness_days = int(provider_config.get("freshness_days", 7))
    perimeter_label = {
        "far": "дальний периметр: бизнес-агенты, агентные продукты, новые функции и тренды",
        "middle": "средний периметр: агентное управление в бизнесе, бизнес-процессы, исследования и кейсы",
        "near": "близкий периметр: агентное управление проектами, портфелями и проектными офисами",
    }.get(perimeter, perimeter)
    prompt_template = provider_config.get(
        "prompt_template",
        (
            "Найди материалы не старше {freshness_days} дней по теме: {query}. "
            "Контекст радара: {perimeter}. Верни только JSON-массив. "
            "Каждый элемент: title, url, date, summary, why_relevant."
        ),
    )
    prompt = prompt_template.format(
        freshness_days=freshness_days,
        query=query,
        perimeter=perimeter_label,
    )
    payload = {
        "model": provider_config.get("model", "sonar"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful web research assistant for an Agentic Project Management radar. "
                    "Use current web sources. Return only valid JSON with source URLs and publication dates."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    base_url = provider_config.get("base_url", "https://api.perplexity.ai").rstrip("/")
    response = requests.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json=payload,
        timeout=int(provider_config.get("timeout_seconds", 90)),
    )
    response.raise_for_status()
    response_data = response.json()
    content = response_data["choices"][0]["message"]["content"]
    content = content.strip()
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
    try:
        rows = json.loads(content)
    except Exception as exc:
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", content)
        if not match:
            return [], f"Perplexity returned non-JSON response: {exc}"
        try:
            rows = json.loads(match.group(1))
        except Exception as nested_exc:
            return [], f"Perplexity returned invalid JSON fragment: {nested_exc}"
    if isinstance(rows, dict):
        rows = rows.get("items", [])
    citations = response_data.get("citations") or response_data.get("search_results") or []
    items: list[Candidate] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        title = strip_text(row.get("title") or query, 220)
        url = row.get("url") or row.get("source") or row.get("link") or ""
        if not url and index < len(citations):
            citation = citations[index]
            if isinstance(citation, str):
                url = citation
            elif isinstance(citation, dict):
                url = citation.get("url") or citation.get("link") or ""
        summary_parts = [row.get("summary") or "", row.get("why_relevant") or ""]
        summary = " ".join(part for part in summary_parts if part).strip()
        published_at = parse_web_research_published_at(
            row.get("date") or row.get("published_at") or row.get("published")
        )
        items.append(
            Candidate(
                title=title,
                url=url,
                source_id=f"web_perplexity_{perimeter}",
                source_title=f"Perplexity fresh web research: {perimeter}",
                source_url=base_url,
                provider="perplexity",
                published_at=published_at,
                summary=make_summary(title, summary),
                raw_excerpt=strip_text(summary, 1000),
                query=query,
            )
        )
    return items, None


def collect_openclaw_cli(query: str, perimeter: str, config: dict[str, Any]) -> tuple[list[Candidate], str | None]:
    limit = int(config["collection"].get("max_search_results_per_query", 10))
    cmd = ["openclaw", "infer", "web", "search", "--json", "--limit", str(limit), "--query", query]
    timeout = int(config.get("web_research", {}).get("providers", {}).get("openclaw_cli", {}).get("timeout_seconds", 90))
    try:
        completed = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return [], "openclaw CLI is not installed"
    except subprocess.CalledProcessError as exc:
        return [], f"openclaw CLI search failed: {exc.stderr or exc.stdout}"
    except subprocess.TimeoutExpired:
        return [], "openclaw CLI search timed out"

    raw = completed.stdout.strip()
    json_start = raw.find("{")
    if json_start > 0:
        raw = raw[json_start:]
    data = json.loads(raw)
    results: list[dict[str, Any]] = []
    for output in data.get("outputs", []):
        result = output.get("result", {})
        results.extend(result.get("results", []))

    items: list[Candidate] = []
    for row in results:
        title = strip_text(row.get("title") or query, 220)
        items.append(
            Candidate(
                title=title,
                url=row.get("url") or "",
                source_id=f"web_openclaw_{perimeter}",
                source_title=f"OpenClaw web research: {perimeter}",
                source_url="openclaw infer web search",
                provider="openclaw_cli",
                published_at=parse_web_research_published_at(row.get("published") or row.get("date")),
                summary=make_summary(title, row.get("description") or ""),
                raw_excerpt=strip_text(row.get("description") or "", 1000),
                query=query,
            )
        )
    return items, None


def collect_source(source: dict[str, Any], config: dict[str, Any]) -> list[Candidate]:
    source_type = source.get("type", "web")
    if source_type == "telegram":
        return collect_telegram(source, config)
    if source_type == "habr_hub":
        return collect_habr(source, config)
    if source_type == "reddit":
        return collect_reddit(source, config)
    if source_type == "arxiv_search":
        return collect_arxiv_search(source, config)
    if source_type == "ai_agents_directory_daily":
        return collect_ai_agents_directory_daily(source, config)
    if source_type == "rss":
        return collect_rss(source["url"], source, config)
    return collect_web_page(source, config)


def collect_web_research(config: dict[str, Any]) -> tuple[list[Candidate], list[str]]:
    web_config = config.get("web_research", {})
    if not web_config.get("enabled", True):
        return [], []
    all_items: list[Candidate] = []
    notes: list[str] = []
    queries = web_config.get("queries", {})
    providers = web_config.get("providers", {})
    default_strategy = web_config.get("strategy", "first_success")
    perimeter_strategy = web_config.get("perimeter_strategy", {})
    for perimeter, query_list in queries.items():
        strategy = perimeter_strategy.get(perimeter, default_strategy)
        first_success = strategy == "first_success"
        for query in query_list:
            query_items_before = len(all_items)
            if providers.get("brave", {}).get("enabled", True):
                try:
                    items, warning = collect_brave(query, perimeter, config)
                    all_items.extend(items)
                    if warning:
                        notes.append(f"Brave skipped for `{query}`: {warning}")
                    if first_success and items:
                        time.sleep(0.2)
                        continue
                except Exception as exc:
                    notes.append(f"Brave failed for `{query}`: {exc}")
            if providers.get("perplexity", {}).get("enabled", True):
                try:
                    items, warning = collect_perplexity(query, perimeter, config)
                    all_items.extend(items)
                    if warning:
                        notes.append(f"Perplexity skipped for `{query}`: {warning}")
                    if first_success and items:
                        time.sleep(0.2)
                        continue
                except Exception as exc:
                    notes.append(f"Perplexity failed for `{query}`: {exc}")
            if providers.get("openclaw_cli", {}).get("enabled", True) and len(all_items) == query_items_before:
                try:
                    items, warning = collect_openclaw_cli(query, perimeter, config)
                    all_items.extend(items)
                    if warning:
                        notes.append(f"OpenClaw CLI search skipped for `{query}`: {warning}")
                except Exception as exc:
                    notes.append(f"OpenClaw CLI search failed for `{query}`: {exc}")
            time.sleep(0.2)
    return all_items, notes


def load_materials(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    materials: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            materials[item["id"]] = item
    return materials


def save_materials(path: Path, materials: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=str(path.parent), text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for item in sorted(materials.values(), key=lambda row: (row.get("published_at") or "", row.get("first_seen_at") or ""), reverse=True):
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp_name, path)


def update_materials(candidates: list[Candidate], materials: dict[str, dict[str, Any]], now: str) -> tuple[list[str], list[str]]:
    new_ids: list[str] = []
    updated_ids: list[str] = []
    created_this_run: set[str] = set()
    for candidate in candidates:
        if not candidate.title and not candidate.url:
            continue
        mid = material_id(candidate.title, candidate.url, candidate.published_at)
        canonical = canonicalize_url(candidate.url)
        hit = {
            "source_id": candidate.source_id,
            "source_title": candidate.source_title,
            "source_url": candidate.source_url,
            "hit_url": candidate.url,
            "provider": candidate.provider,
            "query": candidate.query,
            "seen_at": now,
        }
        if mid not in materials:
            summary = candidate.summary or make_summary(candidate.title, candidate.raw_excerpt)
            materials[mid] = {
                "id": mid,
                "title": candidate.title,
                "url": candidate.url,
                "canonical_url": canonical,
                "published_at": candidate.published_at,
                "first_seen_at": now,
                "last_seen_at": now,
                "summary": summary,
                "raw_excerpt": candidate.raw_excerpt,
                "perimeter": classify_perimeter(candidate.title, summary, candidate.source_title),
                "source_hits": [hit],
                "source_count": 1,
                "hit_count": 1,
            }
            new_ids.append(mid)
            created_this_run.add(mid)
            continue

        item = materials[mid]
        item["last_seen_at"] = now
        item.setdefault("source_hits", []).append(hit)
        unique_sources = sorted({row.get("source_id") for row in item["source_hits"] if row.get("source_id")})
        old_source_count = item.get("source_count", 0)
        item["source_count"] = len(unique_sources)
        item["hit_count"] = len(item["source_hits"])
        if not item.get("published_at") and candidate.published_at:
            item["published_at"] = candidate.published_at
        if candidate.provider == "web_html":
            candidate_summary = candidate.summary or make_summary(candidate.title, candidate.raw_excerpt)
            if len(candidate_summary or "") > len(item.get("summary") or ""):
                item["summary"] = candidate_summary
                item["raw_excerpt"] = candidate.raw_excerpt
                item["perimeter"] = classify_perimeter(candidate.title, candidate_summary, candidate.source_title)
        if old_source_count != item["source_count"] and mid not in created_this_run:
            updated_ids.append(mid)
    return new_ids, updated_ids


def format_item_md(item: dict[str, Any]) -> str:
    date = item.get("published_at") or item.get("first_seen_at") or "дата не определена"
    source_count = item.get("source_count", 0)
    summary = item.get("summary") or "Краткое содержание не сформировано."
    return textwrap.dedent(
        f"""\
        - **{item.get('title', 'Без названия')}**
          - Дата материала: {date}
          - Ссылка: {item.get('url', '')}
          - Периметр: {item.get('perimeter', 'watch')}
          - Источников обнаружения: {source_count}
          - Краткое содержание: {summary}
        """
    ).rstrip()


def write_run_log(wiki_dir: Path, run_id: str, stats: dict[str, Any], materials: dict[str, dict[str, Any]], notes: list[str]) -> Path:
    run_path = wiki_dir / "runs" / f"{run_id}.md"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    new_items = [materials[mid] for mid in stats["new_ids"] if mid in materials]
    updated_items = [materials[mid] for mid in stats["updated_ids"] if mid in materials]
    lines = [
        f"# Запуск AgPM-радара: {run_id}",
        "",
        f"- Время запуска: {stats['started_at']}",
        f"- Найдено кандидатов: {stats['candidate_count']}",
        f"- Новых материалов: {len(new_items)}",
        f"- Материалов с новыми источниками: {len(updated_items)}",
        "",
        "## Новые материалы",
        "",
    ]
    lines.extend([format_item_md(item) + "\n" for item in new_items] or ["Новых материалов не найдено.\n"])
    lines.extend(["", "## Повторно найденные материалы с новыми источниками", ""])
    lines.extend([format_item_md(item) + "\n" for item in updated_items] or ["Таких материалов нет.\n"])
    if notes:
        lines.extend(["", "## Технические заметки", ""])
        lines.extend([f"- {note}" for note in sorted(set(notes))])
    run_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return run_path


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect incremental AgPM radar materials.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--wiki", type=Path, default=DEFAULT_WIKI)
    parser.add_argument("--run-id", default=today_slug())
    parser.add_argument("--no-web-research", action="store_true")
    parser.add_argument("--web-research-only", action="store_true", help="Skip configured sources and run only web research.")
    parser.add_argument(
        "--web-provider",
        choices=["all", "brave", "perplexity", "openclaw_cli"],
        default="all",
        help="Limit web research to one provider.",
    )
    parser.add_argument(
        "--query-set",
        choices=["default", "low_yield_expansion"],
        default="default",
        help="Use a predefined web research query set.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    if args.no_web_research:
        config.setdefault("web_research", {})["enabled"] = False
    if args.query_set == "low_yield_expansion":
        web_config = config.setdefault("web_research", {})
        web_config["enabled"] = True
        web_config["queries"] = LOW_YIELD_EXPANSION_QUERIES
        web_config["strategy"] = "all"
        web_config.setdefault("providers", {}).setdefault("perplexity", {})["freshness_days"] = 14
    if args.web_provider != "all":
        providers = config.setdefault("web_research", {}).setdefault("providers", {})
        for provider_name in ("brave", "perplexity", "openclaw_cli"):
            providers.setdefault(provider_name, {})["enabled"] = provider_name == args.web_provider

    started_at = utc_now()
    materials_path = args.wiki / "data" / "materials.jsonl"
    state_path = args.wiki / "data" / "state.json"
    materials = load_materials(materials_path)
    state = load_state(state_path)
    notes: list[str] = []
    candidates: list[Candidate] = []

    if not args.web_research_only:
        for source in config.get("sources", []):
            if not source.get("enabled", True):
                continue
            try:
                candidates.extend(filter_by_lookback(collect_source(source, config), config))
            except Exception as exc:
                notes.append(f"Source `{source.get('id')}` failed: {exc}")

    web_items, web_notes = collect_web_research(config)
    candidates.extend(filter_by_lookback(web_items, config))
    notes.extend(web_notes)

    new_ids, updated_ids = update_materials(candidates, materials, started_at)
    save_materials(materials_path, materials)
    state.update(
        {
            "last_run_at": started_at,
            "last_run_id": args.run_id,
            "previous_run_at": state.get("last_run_at"),
            "material_count": len(materials),
        }
    )
    save_state(state_path, state)

    stats = {
        "started_at": started_at,
        "candidate_count": len(candidates),
        "new_ids": new_ids,
        "updated_ids": updated_ids,
    }
    run_path = write_run_log(args.wiki, args.run_id, stats, materials, notes)
    stdout_stats = {
        "ok": True,
        "run_log": str(run_path),
        "started_at": started_at,
        "candidate_count": len(candidates),
        "new_count": len(new_ids),
        "updated_count": len(updated_ids),
        "note_count": len(notes),
    }
    print(json.dumps(stdout_stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
