from __future__ import annotations

import os
from dataclasses import dataclass


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    dsn: str
    release_id: str
    capacity_path: str
    user_agent: str
    request_timeout_seconds: float
    connect_timeout_seconds: float
    per_host_interval_seconds: float
    max_body_bytes: int
    min_text_chars: int
    min_free_bytes: int
    lease_seconds: int
    max_attempts: int
    max_in_flight_per_host: int
    respect_robots: bool
    # Defaulted so a caller that does not talk to a model does not have to know
    # these exist. An empty key fails closed at the call, never silently.
    hermes_url: str = "http://127.0.0.1:19700/v1"
    hermes_key: str = ""
    hermes_timeout_seconds: float = 180.0

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            dsn=os.environ.get(
                "RADAR_KX_DSN",
                "dbname=radar_kx user=radar_kx host=/var/run/postgresql",
            ),
            release_id=os.environ.get("RADAR_KX_RELEASE_ID", "development"),
            capacity_path=os.environ.get("RADAR_KX_CAPACITY_PATH", "/var/lib/postgresql"),
            user_agent=os.environ.get(
                "RADAR_KX_USER_AGENT",
                "Radar-KX/1.0 (+https://radar.agpm.space)",
            ),
            request_timeout_seconds=_positive_float("RADAR_KX_REQUEST_TIMEOUT_SECONDS", 30.0),
            connect_timeout_seconds=_positive_float("RADAR_KX_CONNECT_TIMEOUT_SECONDS", 10.0),
            per_host_interval_seconds=_positive_float("RADAR_KX_PER_HOST_INTERVAL_SECONDS", 1.0),
            max_body_bytes=_positive_int("RADAR_KX_MAX_BODY_BYTES", 15 * 1024 * 1024),
            min_text_chars=_positive_int("RADAR_KX_MIN_TEXT_CHARS", 200),
            min_free_bytes=_positive_int("RADAR_KX_MIN_FREE_BYTES", 20 * 1024**3),
            lease_seconds=_positive_int("RADAR_KX_LEASE_SECONDS", 300),
            max_attempts=_positive_int("RADAR_KX_MAX_ATTEMPTS", 4),
            max_in_flight_per_host=_positive_int("RADAR_KX_MAX_IN_FLIGHT_PER_HOST", 8),
            respect_robots=_boolean("RADAR_KX_RESPECT_ROBOTS", True),
            # Loopback to the extraction profile. The orchestrator unit denies
            # every address but localhost, so this is the only reachable model.
            hermes_url=os.environ.get("RADAR_KX_HERMES_URL", "http://127.0.0.1:19700/v1"),
            hermes_key=os.environ.get("RADAR_KX_HERMES_KEY", ""),
            hermes_timeout_seconds=_positive_float("RADAR_KX_HERMES_TIMEOUT_SECONDS", 180.0),
        )
