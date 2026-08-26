"""Ask every prompt the welcome screen can offer, and say which ones refuse.

The welcome pool is assembled from the topic skeleton, so it changes whenever the
base does - and a prompt the base cannot answer is a promise the reader watches
break. `NOT_A_SUBJECT` in `radar_kx.agent_chat` is the list of the ones that did;
this is what produced it and what has to produce it again after the skeleton
moves. There is no rule to compute it from: measured 2026-08-25, every pool topic
sits at L2 and most of the refusing ones are childless leaves.

Runs the real flow, model calls included, so it also warms the answer cache for
exactly the questions the welcome screen offers - the reader who clicks one gets
it back without waiting. Refusals are not cached, so nothing here freezes a
failure.

Read-only against the base; the answers it records are the same rows a reader's
questions would leave.

    RADAR_KX_DSN=... RADAR_KX_HERMES_KEY=... python scripts/pool_sweep.py [--json out.json]

Takes about twenty minutes for a hundred-odd prompts on a warm cache.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from radar_kx.agent_api import AgentService
from radar_kx.agent_chat import pool_prompts, welcome_prompts
from radar_kx.config import Settings
from radar_kx.database import Database


def _pool(service: AgentService) -> list[dict[str, Any]]:
    """The pool the welcome screen samples from - from the same function it uses."""
    topics = {
        f"Расскажи про «{str(topic.get('title') or '').strip()}»": topic
        for topic in service.database.agent_topics()
    }
    items: list[dict[str, Any]] = []
    for prompt in pool_prompts(service.database.agent_topics()):
        topic = topics.get(prompt.text)
        items.append(
            {
                "kind": "topic" if topic else "curated",
                "category": prompt.category,
                "topic_key": topic.get("topic_key") if topic else None,
                "statements": topic.get("statements") if topic else None,
                "text": prompt.text,
            }
        )
    return items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=None)
    arguments = parser.parse_args()

    settings = Settings.from_environment()
    service = AgentService(Database(settings), settings)
    pool = _pool(service)
    # The pool the reader is told the size of, so the two must agree.
    announced = welcome_prompts(service.database.agent_topics())["pool"]
    if announced != len(pool):
        print(
            f"pool disagrees: welcome says {announced}, this walk built {len(pool)}",
            file=sys.stderr,
        )
        return 2

    results: list[dict[str, Any]] = []
    for index, item in enumerate(pool, start=1):
        started = time.time()
        try:
            answered = service.ask(item["text"], client=f"pool-sweep-{index}", admission="all")
            row = {
                **item,
                "refusal": answered.get("refusalReason"),
                "error": answered.get("error"),
                "evidence": len(answered.get("evidence") or []),
                "answer": (answered.get("answer") or "")[:300],
            }
        except Exception as error:  # a sweep reports what broke, it does not stop
            row = {
                **item,
                "refusal": "EXCEPTION",
                "error": f"{type(error).__name__}: {error}"[:300],
            }
        row["seconds"] = round(time.time() - started, 1)
        results.append(row)
        refused = sum(1 for done in results if done.get("refusal"))
        print(f"{index}/{len(pool)}  refused {refused}  {row['seconds']:>5}s  {item['text'][:58]}")
        if arguments.json:
            arguments.json.write_text(
                json.dumps(results, ensure_ascii=False, indent=2, default=float), encoding="utf-8"
            )

    refused = [row for row in results if row.get("refusal")]
    print(f"\npool {len(results)}: answered {len(results) - len(refused)}, refused {len(refused)}")
    for row in refused:
        print(f"  {row.get('topic_key') or row['category']:<40} {row['text'][:60]}")
    if refused:
        print("\nAdd the topic keys above to NOT_A_SUBJECT, or rewrite the curated prompt.")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
