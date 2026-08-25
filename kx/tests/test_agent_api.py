"""Stage 3: what the agent mode serves, and what it refuses to serve.

The service is the first public surface KX has ever had, so these tests are about
the shape of what leaves it: the notices that cannot be dropped, the four levels
arriving together, and an answer never being returned without its evidence.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from radar_kx.agent_api import (
    ASKS_PER_CLIENT,
    LICENCE,
    MACHINE_NOTICE,
    MAX_HITS,
    MAX_QUESTION_CHARS,
    SIGNATURE,
    AgentService,
    AskBudget,
    _int,
    _query,
)
from radar_kx.config import Settings


class FakeDatabase:
    """Answers the six questions the service asks, and records what it was asked."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> Any:
        self.asked.append((name, kwargs))
        return self.answers.get(name)

    def agent_topics(self) -> Any:
        return self._record("agent_topics") or []

    def agent_concept(self, topic_key: str) -> Any:
        return self._record("agent_concept", topic_key=topic_key)

    def agent_observatory(self, **kwargs: Any) -> Any:
        return self._record("agent_observatory", **kwargs) or []

    def agent_gaps(self, **kwargs: Any) -> Any:
        return self._record("agent_gaps", **kwargs) or []

    def agent_pages(self) -> Any:
        return self._record("agent_pages") or []

    def agent_page(self, relative_path: str) -> Any:
        return self._record("agent_page", relative_path=relative_path)

    def agent_search(self, question: str, **kwargs: Any) -> Any:
        return self._record("agent_search", question=question, **kwargs) or []

    def agent_statement(self, claim_id: str) -> Any:
        return self._record("agent_statement", claim_id=claim_id)

    def agent_contradictions(self, **kwargs: Any) -> Any:
        return self._record("agent_contradictions", **kwargs) or (0, [])

    def agent_counts(self) -> Any:
        return self._record("agent_counts") or {}

    def agent_trail(self, claim_id: str) -> Any:
        return self._record("agent_trail", claim_id=claim_id) or []

    def agent_graph(self, **kwargs: Any) -> Any:
        return self._record("agent_graph", **kwargs) or {"centre": None, "nodes": [], "edges": []}

    def cached_answer(self, question: str, **kwargs: Any) -> Any:
        return self._record("cached_answer", question=question, **kwargs)

    def record_answer(self, **kwargs: Any) -> Any:
        return self._record("record_answer", **kwargs) or {}


def service(**answers: Any) -> AgentService:
    settings = Settings.from_environment()
    return AgentService(FakeDatabase(**answers), settings)  # type: ignore[arg-type]


HIT = {
    "claim_id": "c1",
    "statement": "порог автономии определяет границу классов",
    "quote_text": "Порог автономии определяет границу между классами решений.",
    "char_start": 10,
    "char_end": 68,
    "source_url": "https://example.org/a",
    "source_title": "Пороги",
    "material_kind": "fact",
    "admission": "knowledge",
    "status": "observed_signal",
    "primary_source": "",
    "is_retelling": False,
    "shown_on": "2026-06-01",
    "shown_kind": "published",
    "relevance": 0.5,
    "matched_by": ["слова", "смысл"],
    "topics": ["Пороги автономии"],
}


# ---------------------------------------------------------------------------
# The notices that cannot be dropped
# ---------------------------------------------------------------------------


def test_every_generated_answer_carries_the_machine_notice() -> None:
    """Decision 6: the reader is told this is not the base's own editing."""
    answered = service(
        cached_answer={
            "answer_text": "Порог задаёт границу.",
            "refusal_reason": None,
            "evidence_package": [HIT],
            "verification": {"passes": True},
        }
    ).ask("что такое порог автономии")
    assert answered["machineNotice"] == MACHINE_NOTICE
    assert answered["signature"] == SIGNATURE
    assert answered["licence"] == LICENCE


def test_a_refusal_also_carries_the_notice_and_no_answer() -> None:
    answered = service(cached_answer=None, agent_search=[]).ask("вопрос ни о чём")
    assert answered["answer"] is None
    assert answered["refusalReason"] == "no_evidence"
    assert answered["machineNotice"] == MACHINE_NOTICE
    assert answered["evidence"] == []


def test_an_answer_never_arrives_without_its_evidence_field() -> None:
    """Level one and level three are one response, so a client cannot show only one."""
    answered = service(
        cached_answer={
            "answer_text": "Ответ.",
            "refusal_reason": None,
            "evidence_package": [HIT],
            "verification": None,
        }
    ).ask("вопрос")
    assert "evidence" in answered
    assert answered["evidence"]


def test_an_empty_question_is_refused_before_any_retrieval() -> None:
    talking = service()
    assert "error" in talking.ask("   ")
    assert talking.database.asked == []  # type: ignore[attr-defined]


def test_a_pasted_page_is_cut_to_a_question() -> None:
    talking = service(cached_answer=None, agent_search=[])
    talking.ask("я" * (MAX_QUESTION_CHARS * 3))
    question = talking.database.asked[0][1]["question"]  # type: ignore[attr-defined]
    assert len(question) == MAX_QUESTION_CHARS


# ---------------------------------------------------------------------------
# Search, and why something was found
# ---------------------------------------------------------------------------


def test_search_asks_only_for_what_the_reader_asked_for() -> None:
    talking = service(agent_search=[HIT])
    found = talking.search(
        "пороги", filters={"admission": "knowledge", "material_kind": None}, limit=5
    )
    assert found["hits"][0]["matched_by"] == ["слова", "смысл"]
    assert found["licence"] == LICENCE
    name, kwargs = talking.database.asked[0]  # type: ignore[attr-defined]
    assert name == "agent_search"
    assert kwargs["filters"]["admission"] == "knowledge"
    assert kwargs["limit"] == 5


def test_a_search_cannot_ask_for_the_whole_corpus() -> None:
    talking = service(agent_search=[])
    talking.search("пороги", filters={}, limit=10_000)
    assert talking.database.asked[0][1]["limit"] == MAX_HITS  # type: ignore[attr-defined]


def test_an_empty_search_says_so_rather_than_returning_everything() -> None:
    talking = service(agent_search=[HIT])
    assert "error" in talking.search("", filters={}, limit=10)
    assert talking.database.asked == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The shelves
# ---------------------------------------------------------------------------


def test_a_subject_nobody_has_is_a_named_absence_not_an_empty_card() -> None:
    answered = service(agent_concept=None).concept("нет-такой-темы")
    assert answered["error"]
    assert answered["topicKey"] == "нет-такой-темы"


def test_a_concept_card_is_signed_as_machine_assembled() -> None:
    """Decision 3 on signatures: derived pages carry the machine signature."""
    answered = service(agent_concept={"topic_key": "k", "statements": []}).concept("k")
    assert answered["signature"] == SIGNATURE


def test_an_authored_page_is_signed_by_its_author_not_by_the_machine() -> None:
    answered = service(agent_page={"relative_path": "p", "body": "..."}).page("p")
    assert answered["signature"] != SIGNATURE


def test_the_observatory_passes_the_period_and_the_class_through() -> None:
    talking = service(agent_observatory=[])
    talking.observatory(since="2026-06-01", until="2026-08-01", kind="incident")
    _, kwargs = talking.database.asked[0]  # type: ignore[attr-defined]
    assert kwargs == {
        "since": "2026-06-01",
        "until": "2026-08-01",
        "kind": "incident",
        "fresh": False,
    }


# ---------------------------------------------------------------------------
# Small things that would be bugs at the edge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"), [("5", 5), (None, 9), ("", 9), ("-3", 1), ("не число", 9)]
)
def test_a_limit_from_a_url_is_never_zero_or_a_crash(given: str | None, expected: int) -> None:
    assert _int(given, 9) == expected


def test_query_parsing_takes_the_first_value_of_a_repeated_parameter() -> None:
    assert _query("/search?q=один&q=два&limit=5") == {"q": "один", "limit": "5"}


def test_a_statement_nobody_has_is_a_named_absence() -> None:
    answered = service(agent_statement=None).statement("нет-такого")
    assert answered["error"]


def test_the_four_levels_arrive_together_for_one_statement() -> None:
    answered = service(
        agent_statement={
            "claim_id": "c1",
            "statement": "утверждение",
            "quote_text": "цитата",
            "char_start": 1,
            "char_end": 7,
            "source_url": "https://example.org/a",
            "material_kind": "fact",
            "status": "canon",
            "topics": [{"topic_key": "k"}],
            "links": [],
        }
    ).statement("c1")
    assert answered["statement"]
    assert answered["quote_text"]
    assert answered["char_start"] == 1
    assert answered["source_url"].startswith("https://")
    assert answered["material_kind"] == "fact"


def test_the_response_is_json_a_browser_can_read() -> None:
    answered = service(agent_topics=[{"topic_key": "k", "title": "Т", "statements": 3}]).topics()
    assert json.loads(json.dumps(answered, ensure_ascii=False, default=str))["topics"]


# ---------------------------------------------------------------------------
# What stands between a `for` loop and the model bill
# ---------------------------------------------------------------------------


def test_a_client_asking_too_fast_is_refused_without_a_model_call() -> None:
    """`/ask` reaches a paid model with no login in front of it."""
    talking = service(cached_answer=None, agent_search=[])
    for _ in range(ASKS_PER_CLIENT):
        talking.ask("вопрос " + str(_), client="1.2.3.4")
    calls_before = len(talking.database.asked)  # type: ignore[attr-defined]
    answered = talking.ask("ещё один вопрос", client="1.2.3.4")
    assert answered["refusalReason"] == "rate_limited_client"
    assert answered["machineNotice"] == MACHINE_NOTICE
    # And it cost nothing: no retrieval, so no model call behind it.
    assert len(talking.database.asked) == calls_before + 1  # type: ignore[attr-defined]


def test_another_client_is_not_punished_for_the_first_one() -> None:
    talking = service(cached_answer=None, agent_search=[])
    for index in range(ASKS_PER_CLIENT):
        talking.ask(f"вопрос {index}", client="1.2.3.4")
    answered = talking.ask("свежий вопрос", client="5.6.7.8")
    assert answered["refusalReason"] != "rate_limited_client"


def test_the_day_has_no_ceiling_unless_one_is_asked_for() -> None:
    """The owner's call: the point of the base is that people use it.

    A limit that turns the agent off at four in the afternoon is a worse failure
    than the bill it was guarding. The mechanism stays - `per_day` still bounds
    when a number is given - and the default is off.
    """
    from radar_kx.agent_api import DAILY_ASK_BUDGET

    assert DAILY_ASK_BUDGET == 0
    unbounded = AskBudget(per_client=1000, window=300.0)
    assert all(unbounded.refused(f"client-{n}") is None for n in range(50))

    # And it is still a mechanism, not a deleted one.
    bounded = AskBudget(per_client=1000, window=300.0, per_day=3)
    assert bounded.refused("a") is None
    assert bounded.refused("b") is None
    assert bounded.refused("c") is None
    assert bounded.refused("d") == "today"


def test_the_budget_resets_the_next_day() -> None:
    budget = AskBudget(per_client=1, window=300.0, per_day=1)
    day_one = 1_700_000_000.0
    assert budget.refused("a", now=day_one) is None
    assert budget.refused("a", now=day_one + 60) == "today"
    assert budget.refused("a", now=day_one + 86_400 * 2) is None


def test_a_cached_answer_costs_nothing_and_is_never_charged() -> None:
    """The same question twice is one model call; the budget must not say otherwise."""
    talking = service(
        cached_answer={
            "answer_text": "Ответ.",
            "refusal_reason": None,
            "evidence_package": [HIT],
            "verification": None,
        }
    )
    for _ in range(ASKS_PER_CLIENT * 3):
        answered = talking.ask("один и тот же вопрос", client="1.2.3.4")
        assert answered["answer"] == "Ответ."
        assert answered["fromCache"] is True


def test_a_limit_from_a_url_cannot_be_raised_without_bound() -> None:
    """`?limit=99999999` returned a whole table before there was a ceiling."""
    assert _int("99999999", 10) == MAX_HITS
    assert _int("7", 10) == 7
    assert _int("0", 10) == 1


def test_the_reader_chooses_which_half_of_the_base_is_searched() -> None:
    """Decision 3 keeps knowledge and the chronicle apart; the chip picks one.

    The chips used to be decorative: the client never sent the choice and the
    service hard-coded `knowledge`, so selecting "хронике рынка" and asking about
    a market event returned a refusal with the chip lit.
    """
    talking = service(cached_answer=None, agent_search=[])
    talking.ask("что случилось на рынке", admission="observatory")
    name, kwargs = talking.database.asked[1]  # type: ignore[attr-defined]
    assert name == "agent_search"
    assert kwargs["filters"]["admission"] == "observatory"


def test_an_admission_nobody_named_does_not_widen_the_search() -> None:
    talking = service(cached_answer=None, agent_search=[])
    talking.ask("вопрос", admission="everything")
    _, kwargs = talking.database.asked[1]  # type: ignore[attr-defined]
    assert kwargs["filters"]["admission"] == "knowledge"


# ---------------------------------------------------------------------------
# What a public endpoint owes a caller when it cannot answer
# ---------------------------------------------------------------------------


def test_an_answer_the_service_cannot_read_still_gets_a_reply() -> None:
    """`parse_answer` raises `ValueError`, and only `OrchestratorError` was caught.

    An unparseable model reply therefore raised through
    `BaseHTTPRequestHandler`, which logs a traceback and closes the socket: the
    caller got no status line at all, and the site showed a network error rather
    than a failure. A public endpoint owes an answer even when the answer is no.
    """
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from radar_kx.agent_api import AgentService, make_handler

    class Unreadable:
        """A service whose model said something the protocol cannot read."""

        def ask(
            self,
            question: str,
            *,
            client: str,
            admission: str = "knowledge",
            asks_per_client: int | None = None,
        ) -> dict[str, Any]:
            raise ValueError("answer contains no JSON object")

        def access(self, authorization: str | None) -> dict[str, Any]:
            return {"valid": False, "plan": None, "expiresAt": None}

    handler = make_handler(cast(AgentService, Unreadable()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
        connection.request(
            "POST",
            "/ask",  # Caddy serves this on /kb/ask
            body=json.dumps({"question": "вопрос"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 503
    assert body["error"]
    # And nothing of the traceback reaches the caller.
    assert "JSON object" not in json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# UC-01: the search has three arms, and one of them was never given anything
# ---------------------------------------------------------------------------


def test_the_reader_search_reaches_for_the_meaning_arm() -> None:
    """`ask` passed a question vector from the first day; `search` never did.

    The arm excludes itself when the vector is NULL - `WHERE question_vector IS
    NOT NULL` - so the endpoint the reader would use ran on words alone while
    the documentation, the labels and the arm's own name all said otherwise.
    Measured on production before the fix: the same phrase through `ask` came
    back "слова, смысл" and about delegated autonomy, through `search` "слова"
    and about whatever shared a stem.
    """
    reading = service(agent_search=[HIT])
    reading.search("что мешает доверять автономным исполнителям", filters={}, limit=5)
    called = [call for call in reading.database.asked if call[0] == "agent_search"]  # type: ignore[attr-defined]
    assert called, "the search never reached the database"
    assert "question_vector" in called[0][1], (
        "search must offer the meaning arm a vector, the way ask does"
    )


def test_the_contradictions_route_answers_with_both_sides() -> None:
    """A pair is the finding. One side of it is just a statement."""
    pair = {"from_id": "a", "to_id": "b", "first_statement": "растёт", "second_statement": "падает"}
    reading = service(agent_contradictions=(283, [pair]))
    answered = reading.contradictions(60)
    assert answered["total"] == 283
    assert answered["pairs"][0]["first_statement"] == "растёт"
    assert answered["pairs"][0]["second_statement"] == "падает"
    assert answered["signature"] == SIGNATURE


# ---------------------------------------------------------------------------
# ADR-0011: числа объектов базы открыты всем, содержание - нет
# ---------------------------------------------------------------------------


COUNTS = {
    "statements": 11759,
    "knowledge": 8086,
    "observatory": 3673,
    "topics": 229,
    "entities": 6288,
    "links": 45836,
    "contradictions": 797,
    "pages": 58,
    "gaps": 338,
    "traceable": 6372,
}


def test_counts_reach_a_reader_without_a_key() -> None:
    """Решение владельца: число - не содержание, и оно публично."""
    assert service(agent_counts=COUNTS).counts() == COUNTS


def test_counts_are_counted_once_and_kept() -> None:
    """Полсекунды по всей поверхности - не та цена, чтобы платить её на каждый
    запрос: база меняется раз в сутки."""
    api = service(agent_counts=COUNTS)
    api.counts()
    api.counts()
    api.counts()
    asked = [name for name, _ in cast(Any, api.database).asked if name == "agent_counts"]
    assert len(asked) == 1


def test_the_welcome_screen_carries_the_counts_with_its_examples() -> None:
    """Диалог и так ходит за примерами: второй круг ради счётчиков был бы лишним."""
    served = service(agent_counts=COUNTS, agent_topics=[]).prompts()
    assert served["counts"] == COUNTS


def test_a_statement_says_which_issue_carried_its_material() -> None:
    """Цепочка доверия кончалась первоисточником; последнее звено - выпуск."""
    trail = [
        {
            "issue_date": "2026-08-24",
            "issue_number": 79,
            "perimeter": "near",
            "material_title": "Казначейство США вводит правила автономных платежей",
            "material_url": "https://example.org/a",
            "key_material": True,
            "source_count": 1,
        }
    ]
    served = service(agent_statement={"claim_id": "c1"}, agent_trail=trail).statement("c1")
    assert served["trail"] == trail


def test_a_statement_that_never_came_from_an_issue_says_so_by_being_empty() -> None:
    """46 % утверждений пришли из канона и wiki. Пустой путь - ответ, не пробел."""
    served = service(agent_statement={"claim_id": "c1"}).statement("c1")
    assert served["trail"] == []


def test_a_missing_statement_is_not_given_a_trail_to_nowhere() -> None:
    served = service().statement("нет-такого")
    assert "trail" not in served
    assert served["error"]
