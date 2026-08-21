from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

import httpx

from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.fetcher import DocumentTask, FetchResult, HostLimiter, RobotsPolicy, fetch_document


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def run_until_idle(settings: Settings, *, workers: int) -> dict[str, Any]:
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    database = Database(settings)
    limiter = HostLimiter(settings.per_host_interval_seconds)
    robots = RobotsPolicy()
    timeout = httpx.Timeout(
        timeout=settings.request_timeout_seconds,
        connect=settings.connect_timeout_seconds,
    )
    limits = httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers)
    processed = 0
    with (
        httpx.Client(
            timeout=timeout,
            limits=limits,
            headers={"user-agent": settings.user_agent, "accept": "*/*"},
            follow_redirects=False,
            trust_env=False,
        ) as client,
        ThreadPoolExecutor(max_workers=workers, thread_name_prefix="radar-kx") as executor,
    ):
        future_map: dict[Future[FetchResult], DocumentTask] = {}
        while True:
            available_slots = workers - len(future_map)
            if available_slots:
                tasks = database.claim_tasks(
                    limit=available_slots,
                    per_host_limit=settings.max_in_flight_per_host,
                )
                for task in tasks:
                    future = executor.submit(
                        fetch_document,
                        task=task,
                        client=client,
                        limiter=limiter,
                        robots=robots,
                        settings=settings,
                    )
                    future_map[future] = task
            if not future_map:
                break
            completed, _ = wait(tuple(future_map), return_when=FIRST_COMPLETED)
            for future in completed:
                task = future_map.pop(future)
                try:
                    result = future.result()
                    recorded = database.record_fetch_result(result)
                    processed += 1
                    _log("document_processed", **recorded, processed=processed)
                except Exception as exc:
                    _log(
                        "worker_fatal",
                        documentId=task.document_id,
                        errorType=type(exc).__name__,
                        error=str(exc)[:4000],
                    )
                    raise
    status = database.status()
    _log("worker_idle", processed=processed, status=status)
    return {"processed": processed, "status": status}
