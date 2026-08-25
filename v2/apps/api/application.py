"""Same-origin API, SPA and immutable gazette routing for Radar V2."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Final
from urllib.parse import parse_qsl, urlsplit

from packages.storage.safe_files import SafeFilesystemError, open_directory_nofollow, relative_parts

from apps.api.database import PublicDatabaseError
from apps.api.service import ApiResponse, RadarApi

_MAX_STATIC_BYTES: Final = 8 * 1024 * 1024
_GAZETTE_PERIOD: Final = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_SPA_ISSUE: Final = re.compile(r"^/issues/[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_NO_STORE: Final = (("Cache-Control", "no-store"),)
_IMMUTABLE_CACHE: Final = (("Cache-Control", "public, max-age=31536000, immutable"),)
_SECURITY_HEADERS: Final = (
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
)
_SPA_CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
_GAZETTE_CSP: Final = (
    "default-src 'none'; script-src 'none'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)
# Gazette issues bundled with the application itself; the publisher's own
# releases live under /gazettes/ with manifest-verified assets.
_BUNDLED_GAZETTE_ISSUES: Final[frozenset[str]] = frozenset(
    {"/gazette-20260803.html", "/gazette-20260901.html"}
)
_CONTENT_TYPES: Final = {
    ".css": "text/css; charset=utf-8",
    ".gif": "image/gif",
    ".html": "text/html; charset=utf-8",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".webp": "image/webp",
}


class StaticRouteError(RuntimeError):
    """A static route is invalid, absent or outside its immutable root."""


def _read_static(root: Path, relative: str) -> bytes:
    try:
        parts = relative_parts(relative)
        directory = open_directory_nofollow(root)
    except (OSError, SafeFilesystemError) as error:
        raise StaticRouteError("static path is invalid") from error
    current = directory
    try:
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            if current != directory:
                os.close(current)
            current = child
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=current,
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > _MAX_STATIC_BYTES
                or stat.S_IMODE(before.st_mode) & 0o022
            ):
                raise StaticRouteError("static file invariants failed")
            chunks: list[bytes] = []
            remaining = _MAX_STATIC_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if before != after or remaining <= 0:
                raise StaticRouteError("static file changed or exceeded its size bound")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except (FileNotFoundError, NotADirectoryError, OSError) as error:
        raise StaticRouteError("static file is unavailable") from error
    finally:
        if current != directory:
            os.close(current)
        os.close(directory)


def _response(
    status: int,
    body: bytes,
    *,
    content_type: str,
    cache: tuple[tuple[str, str], ...],
    csp: str,
) -> ApiResponse:
    return ApiResponse(
        status=status,
        body=body,
        headers=(
            ("Content-Type", content_type),
            *cache,
            ("Content-Security-Policy", csp),
            *_SECURITY_HEADERS,
        ),
    )


def _not_found() -> ApiResponse:
    return _response(
        404,
        b"Not found\n",
        content_type="text/plain; charset=utf-8",
        cache=_NO_STORE,
        csp="default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    )


class RadarApplication:
    """Route `/api`, exact assets, SPA routes and published gazette trees separately."""

    def __init__(self, api: RadarApi, *, web_root: Path, gazette_root: Path) -> None:
        self.api = api
        self.web_root = web_root
        self.gazette_root = gazette_root

    def _gazette_asset(self, period: str, relative: str) -> tuple[str, int, str] | None:
        candidates = (relative, f"{period}/{relative}", f"gazettes/{period}/{relative}")
        try:
            rows = self.api.manager.execute(
                lambda connection, _identity: connection.execute(
                    """
                    SELECT a.sha256, a.bytes, a.media_type
                    FROM pub_gazettes_v1 AS g
                    JOIN pub_gazette_assets_v1 AS a ON a.gazette_id = g.gazette_id
                    WHERE g.period = ? AND a.relative_path IN (?, ?, ?)
                    ORDER BY g.gazette_id, a.relative_path
                    LIMIT 2
                    """,
                    (period, *candidates),
                ).fetchall()
            )
        except (PublicDatabaseError, sqlite3.DatabaseError):
            return None
        if len(rows) != 1:
            return None
        return str(rows[0][0]), int(rows[0][1]), str(rows[0][2])

    def handle(
        self,
        method: str,
        raw_target: str,
        *,
        request_id: str | None = None,
        remote_key: str = "local",
    ) -> ApiResponse:
        """Handle one same-origin request with no SPA fallback for real file routes."""
        parsed = urlsplit(raw_target)
        if parsed.path.startswith("/api/") or parsed.path == "/api":
            return self.api.handle(
                method,
                raw_target,
                request_id=request_id,
                remote_key=remote_key,
            )
        if (
            method != "GET"
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or "%" in parsed.path
        ):
            return _not_found()
        try:
            query = dict(
                parse_qsl(
                    parsed.query,
                    keep_blank_values=True,
                    encoding="utf-8",
                    errors="strict",
                    max_num_fields=2,
                )
            )
        except (UnicodeDecodeError, ValueError):
            return _not_found()
        if parsed.path in {
            "/assets/styles.css",
            "/assets/app.mjs",
            "/assets/fonts/GolosText[wght].ttf",
            "/assets/fonts/PTMono-Regular.ttf",
        }:
            if set(query) - {"v"} or ("v" in query and not query["v"]):
                return _not_found()
            name = parsed.path.removeprefix("/assets/")
            try:
                body = _read_static(self.web_root, name)
            except StaticRouteError:
                return _not_found()
            return _response(
                200,
                body,
                content_type=_CONTENT_TYPES[Path(name).suffix],
                cache=_IMMUTABLE_CACHE,
                csp=_SPA_CSP,
            )
        if parsed.path.startswith("/assets/"):
            return _not_found()
        if parsed.path in _BUNDLED_GAZETTE_ISSUES:
            if query:
                return _not_found()
            try:
                body = _read_static(self.web_root, parsed.path.removeprefix("/"))
            except StaticRouteError:
                return _not_found()
            return _response(
                200,
                body,
                content_type="text/html; charset=utf-8",
                cache=_IMMUTABLE_CACHE,
                csp=_GAZETTE_CSP,
            )
        if parsed.path.startswith("/gazettes/"):
            if query:
                return _not_found()
            tail = parsed.path.removeprefix("/gazettes/")
            parts = tail.split("/")
            if not parts or _GAZETTE_PERIOD.fullmatch(parts[0]) is None:
                return _not_found()
            period = parts[0]
            relative_tail = "/".join(parts[1:])
            if not relative_tail:
                relative_tail = "index.html"
            if parsed.path.endswith("/") and relative_tail != "index.html":
                return _not_found()
            expected = self._gazette_asset(period, relative_tail)
            if expected is None:
                return _not_found()
            relative = f"{period}/{relative_tail}"
            try:
                body = _read_static(self.gazette_root, relative)
            except StaticRouteError:
                return _not_found()
            suffix = Path(relative).suffix.lower()
            content_type = _CONTENT_TYPES.get(suffix)
            expected_sha256, expected_bytes, expected_media_type = expected
            if (
                content_type is None
                or len(body) != expected_bytes
                or hashlib.sha256(body).hexdigest() != expected_sha256
                or content_type.partition(";")[0] != expected_media_type.partition(";")[0].lower()
            ):
                return _not_found()
            return _response(
                200,
                body,
                content_type=content_type,
                cache=_IMMUTABLE_CACHE,
                csp=_GAZETTE_CSP,
            )
        spa_route = parsed.path in {"/", "/gazettes", "/issues", "/search"} or (
            _SPA_ISSUE.fullmatch(parsed.path) is not None
        )
        if not spa_route or query:
            return _not_found()
        try:
            body = _read_static(self.web_root, "index.html")
        except StaticRouteError:
            return _not_found()
        return _response(
            200,
            body,
            content_type="text/html; charset=utf-8",
            cache=_NO_STORE,
            csp=_SPA_CSP,
        )


__all__ = ["RadarApplication", "StaticRouteError"]
