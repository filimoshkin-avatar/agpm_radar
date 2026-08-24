"""The conversation layer: welcome prompts, deterministic tools, sessions.

What leaves the service in a conversation turn: stages the pipeline actually
passed through, a tool card chosen from the question rather than guessed by a
model, and a session label that travels back untouched - stored nowhere.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any

from radar_kx.agent_api import AgentService, make_handler
from radar_kx.agent_chat import select_tool, valid_session, welcome_prompts


def service(**answers: Any) -> AgentService:
    """The same fixture the agent API tests use, imported from their module."""
    from test_agent_api import service as api_service

    return api_service(**answers)


TOPICS = [
    {
        "topic_key": "t-autonomy",
        "title": "Пороги автономии",
        "level": 2,
        "path": "01/02",
        "statements": 12,
    },
    {
        "topic_key": "t-audit",
        "title": "Аудит действий агентов",
        "level": 2,
        "path": "01/03",
        "statements": 7,
    },
    {
        "topic_key": "t-thin",
        "title": "Редкая тема",
        "level": 3,
        "path": "01/04",
        "statements": 1,
    },
]

CACHED: dict[str, Any] = {
    "answer_text": "Порог задаёт границу.",
    "refusal_reason": None,
    "evidence_package": [],
    "verification": {"passes": True},
}


# ---------------------------------------------------------------------------
# Welcome prompts
# ---------------------------------------------------------------------------


def test_the_same_seed_offers_the_same_prompts() -> None:
    """A sampled screen must be reproducible: a seed fixes what was offered."""
    assert welcome_prompts(TOPICS, seed=7) == welcome_prompts(TOPICS, seed=7)


def test_the_pool_follows_the_base() -> None:
    """Concepts the base holds become prompts; thin subjects do not."""
    pool = welcome_prompts(TOPICS, seed=1)
    assert pool["pool"] == pool["poolCurated"] + 2  # «Редкая тема» stays out
    texts = " ".join(prompt["text"] for prompt in pool["prompts"])
    assert "Редкая тема" not in texts


def test_no_category_takes_over_the_welcome_screen() -> None:
    pool = welcome_prompts(TOPICS, seed=3)
    categories = [prompt["category"] for prompt in pool["prompts"]]
    assert len(categories) == 6
    assert categories.count("concept") <= 2


# ---------------------------------------------------------------------------
# Tool selection
# ---------------------------------------------------------------------------


def test_a_named_subject_beats_a_keyword() -> None:
    choice = select_tool("есть ли противоречия про пороги автономии?", TOPICS)
    assert choice.tool == "concept"
    assert choice.topic_key == "t-autonomy"
    assert choice.because == "topic"


def test_contradiction_and_market_questions_get_their_cards() -> None:
    assert select_tool("где база видит разногласия об эффекте?", TOPICS).tool == "contra"
    assert select_tool("какая доля команд использует агентов?", TOPICS).tool == "watch"


def test_everything_else_is_the_verified_evidence_pipeline() -> None:
    assert select_tool("что считается первоисточником?", TOPICS).tool == "find"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_a_session_is_a_label_not_a_permission() -> None:
    assert valid_session("")
    assert valid_session("abc-123_XY")
    assert not valid_session("bad session")
    assert not valid_session("x" * 65)


# ---------------------------------------------------------------------------
# A conversation turn
# ---------------------------------------------------------------------------


def test_a_turn_carries_its_stages_its_card_and_the_session_untouched() -> None:
    stages, payload = service(
        cached_answer=CACHED,
        agent_topics=TOPICS,
        agent_concept={"topicKey": "t-autonomy", "statements": []},
    ).chat("что говорят пороги автономии?", client="tester", session="s-42")
    assert stages == [{"step": "search", "done": True, "hits": 0, "cache": True}]
    assert payload["session"] == "s-42"
    assert payload["stages"] == stages
    assert payload["tool"] == "concept"
    assert payload["toolCards"][0]["type"] == "concept"


def test_a_bogus_session_is_refused_before_anything_runs() -> None:
    stages, payload = service(agent_topics=TOPICS).chat("вопрос", session="не-то")
    assert stages == []
    assert "error" in payload


def test_the_stream_shows_the_conveyor_and_answers_last() -> None:
    events = list(
        service(cached_answer=CACHED, agent_topics=TOPICS).chat_events(
            "что такое канон?", client="tester"
        )
    )
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "stage"
    assert kinds[-1] == "result"
    answer = events[-1][1]
    assert answer["machineNotice"]
    # A stage only ever arrives done: an unfinished stage is not a fact.
    for _, stage in events[:-1]:
        assert stage["done"] is True


# ---------------------------------------------------------------------------
# The endpoints, over real HTTP
# ---------------------------------------------------------------------------


def _serve(answered: AgentService) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(answered))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_prompts_chat_and_stream_routes() -> None:
    server, thread = _serve(service(cached_answer=CACHED, agent_topics=TOPICS))
    try:
        host, port = server.server_address[0], server.server_address[1]
        connection = HTTPConnection(str(host), int(port), timeout=5)

        connection.request("GET", "/prompts?seed=5")
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert len(body["prompts"]) == 6

        connection.request(
            "POST",
            "/chat",
            body=json.dumps({"question": "что говорят пороги автономии?", "session": "s-1"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert body["session"] == "s-1"
        assert body["tool"] == "concept"

        connection.request(
            "POST",
            "/chat/stream",
            body=json.dumps({"question": "что говорят пороги автономии?", "session": "s-1"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
        raw = response.read().decode("utf-8")
        assert "event: stage" in raw
        assert raw.index("event: stage") < raw.index("event: result")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
