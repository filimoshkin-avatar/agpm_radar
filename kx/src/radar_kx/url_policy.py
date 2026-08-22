from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
    "yclid",
    "ysclid",
}


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for the production fetcher."""


@dataclass(frozen=True, slots=True)
class ResolvedUrl:
    url: str
    host: str
    addresses: tuple[str, ...]


def normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError("only http and https URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URL user information is forbidden")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeUrlError("URL host is required")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError("URL host is not valid IDNA") from exc
    port = parsed.port
    if port is not None and not (1 <= port <= 65535):
        raise UnsafeUrlError("URL port is invalid")
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query.append((key, item))
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


#: Documents that were never on the web still need a canonical URL, because
#: ``document_id`` is sha256 over one. The AgPM canon gets this reserved scheme:
#: it cannot collide with a fetched page, it cannot be mistaken for one, and
#: migration 003 constrains ``documents.canonical_url`` to http(s) or this.
CANON_URL_SCHEME = "agpm-canon"


def canon_url(relative_path: str) -> str:
    """Build the canonical URL of a local canon document from its path under ``raw/``."""
    cleaned = relative_path.strip().strip("/")
    if not cleaned:
        raise UnsafeUrlError("canon document path is required")
    parts = cleaned.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafeUrlError(f"canon document path is not relative: {relative_path!r}")
    return f"{CANON_URL_SCHEME}:/{'/'.join(parts)}"


def canonical_identity_url(value: str) -> str:
    """Normalize whatever addresses a document, on the web or on this host.

    http(s) goes through the ordinary normalizer so a document keeps one identity
    across every store. The reserved canon scheme is validated and passed through
    unchanged - running it through ``normalize_url`` would reject it, and there is
    nothing about a local path to normalize.
    """
    candidate = value.strip()
    if candidate.lower().startswith(f"{CANON_URL_SCHEME}:"):
        return canon_url(candidate[len(CANON_URL_SCHEME) + 1 :])
    return normalize_url(candidate)


def resolve_public_url(value: str) -> ResolvedUrl:
    normalized = normalize_url(value)
    parsed = urlsplit(normalized)
    host = parsed.hostname
    if host is None:
        raise UnsafeUrlError("URL host is required")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrlError(f"DNS resolution failed: {exc}") from exc
    addresses = tuple(sorted({str(result[4][0]) for result in results}))
    if not addresses:
        raise UnsafeUrlError("DNS resolution returned no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise UnsafeUrlError(f"non-public address is forbidden: {address}")
    return ResolvedUrl(url=normalized, host=host, addresses=addresses)


def reddit_json_url(value: str) -> str | None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host not in {"reddit.com", "www.reddit.com", "old.reddit.com"}:
        return None
    if "/comments/" not in parsed.path:
        return None
    path = parsed.path.rstrip("/")
    if not path.endswith(".json"):
        path += ".json"
    return urlunsplit((parsed.scheme, "www.reddit.com", path, "raw_json=1", ""))


def telegram_embed_url(value: str) -> str | None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "telegram.me", "www.t.me"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or not parts[-1].isdigit():
        return None
    return urlunsplit((parsed.scheme, "t.me", parsed.path, "embed=1&mode=tme", ""))
