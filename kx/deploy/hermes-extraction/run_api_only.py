#!/usr/bin/env python3
"""Start the pinned Hermes API gateway without cron or gateway housekeeping.

Adapted from the nrd-intake profile's entry point on this host, which is the
precedent ADR-0005 §8 names. Four things it does, all of which the extraction
profile needs for the same reasons NRD needed them:

* the cron scheduler is replaced by an inert one, so nothing in this profile can
  schedule itself work;
* gateway housekeeping is disabled, so the process does only what a request asks;
* agents are ephemeral - no session database is opened, so no transcript of other
  people's documents accumulates in a second place outside the evidence store;
* the model allowlist is installed. Hermes accepts an arbitrary model identifier in
  a request body even with ``model_routes`` configured, so without this patch P9's
  two-model limit is a convention rather than a control.

The allowlist is built from this profile's own ``config.yaml``, so the routes and
the allowlist cannot drift apart. ``verify_profile.py`` checks that those routes are
still exactly the two models the owner approved, and refuses to start otherwise.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml


class DisabledCronScheduler:
    """Inert scheduler selected only by the Radar KX service entrypoint."""

    @property
    def name(self) -> str:
        return "radar-kx-disabled"

    def is_available(self) -> bool:
        return True

    def start(
        self,
        stop_event: threading.Event,
        *,
        adapters: Any = None,
        loop: Any = None,
        interval: int = 60,
    ) -> None:
        del adapters, loop, interval
        stop_event.wait()

    def stop(self) -> None:
        return None


def disabled_housekeeping(
    stop_event: threading.Event,
    adapters: Any = None,
    loop: Any = None,
    interval: int = 60,
) -> None:
    del adapters, loop, interval
    stop_event.wait()


def install_ephemeral_api_agents(api_server_module: Any) -> None:
    """Disable transcript/FTS persistence before any Radar agent is created."""
    # ProcessRegistry imports delegation recovery even when every API toolset is
    # disabled. For this one-shot profile there can be no delegated work to
    # recover; suppress that import-time state.db open before AIAgent loads tools.
    import tools.async_delegation as async_delegation  # type: ignore[import-not-found]

    def no_delegation_recovery(queue: Any) -> None:
        del queue
        return None

    async_delegation.restore_undelivered_completions = no_delegation_recovery

    adapter = api_server_module.APIServerAdapter
    original = adapter._create_agent
    if getattr(original, "_radar_ephemeral", False):
        return

    def no_session_db(self: Any) -> None:
        del self
        return None

    async def no_session_db_async(self: Any) -> None:
        del self
        return None

    # _create_agent passes _ensure_session_db() into AIAgent. Patch both sync
    # and request-handler entry points first so SessionDB is never opened,
    # initialized, cached or closed-and-left-in-cache.
    adapter._ensure_session_db = no_session_db
    adapter._ensure_session_db_async = no_session_db_async

    def create_ephemeral_agent(self: Any, *args: Any, **kwargs: Any) -> Any:
        agent = original(self, *args, **kwargs)
        agent._session_db = None
        agent._persist_disabled = True
        return agent

    create_ephemeral_agent._radar_ephemeral = True  # type: ignore[attr-defined]
    adapter._create_agent = create_ephemeral_agent


def install_radar_model_allowlist(api_server_module: Any) -> None:
    """Allow only aliases declared by this environment's profile config."""
    profile_home = Path(os.environ.get("HERMES_HOME", ""))
    try:
        config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
        routes = config["platforms"]["api_server"]["extra"]["model_routes"]
    except Exception as exc:
        raise SystemExit(78) from exc
    if not isinstance(routes, dict) or not routes:
        raise SystemExit(78)
    allowed_models = frozenset(routes)
    adapter = api_server_module.APIServerAdapter
    for method_name in ("_handle_chat_completions", "_handle_responses", "_handle_runs"):
        original = getattr(adapter, method_name)
        if getattr(original, "_radar_model_allowlist", False):
            continue

        async def restricted(self: Any, request: Any, _original: Any = original) -> Any:
            try:
                body = await request.json()
            except Exception:
                return await _original(self, request)
            if isinstance(body, dict):
                requested_model = body.get("model")
                if requested_model is not None and requested_model not in allowed_models:
                    return api_server_module.web.json_response(
                        api_server_module._openai_error(
                            "Requested model is not allowed by the Radar KX extraction profile",
                            code="radar_kx_model_not_allowed",
                        ),
                        status=400,
                    )
                if body.get("provider") is not None or body.get("model_options") is not None:
                    return api_server_module.web.json_response(
                        api_server_module._openai_error(
                            "Direct provider or model options are not allowed by"
                            " the Radar KX extraction profile",
                            code="radar_kx_runtime_override_not_allowed",
                        ),
                        status=400,
                    )
            return await _original(self, request)

        restricted._radar_model_allowlist = True  # type: ignore[attr-defined]
        setattr(adapter, method_name, restricted)


def plan_gateway_stop(pid_text: str) -> None:
    """Mark a clean stop and wait so systemd does not race the marker watcher."""
    if not pid_text.isascii() or not pid_text.isdecimal():
        raise SystemExit(78)
    pid = int(pid_text)
    if pid <= 1:
        raise SystemExit(78)
    from gateway.status import write_planned_stop_marker  # type: ignore[import-not-found]

    if not write_planned_stop_marker(pid):
        raise SystemExit(75)
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise SystemExit(75) from exc
        time.sleep(0.1)
    raise SystemExit(75)


def main(argv: tuple[str, ...] = ()) -> None:
    if argv:
        if len(argv) == 2 and argv[0] == "--plan-stop":
            plan_gateway_stop(argv[1])
            return
        raise SystemExit(78)
    import cron.scheduler_provider as scheduler_provider  # type: ignore[import-not-found]
    import gateway.platforms.api_server as api_server  # type: ignore[import-not-found]
    import gateway.run as gateway_run  # type: ignore[import-not-found]

    scheduler_provider.resolve_cron_scheduler = DisabledCronScheduler
    gateway_run._start_gateway_housekeeping = disabled_housekeeping
    install_ephemeral_api_agents(api_server)
    install_radar_model_allowlist(api_server)
    gateway_run.main()


if __name__ == "__main__":
    main(tuple(sys.argv[1:]))
