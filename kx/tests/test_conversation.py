"""Conversation grounding, source opt-in and cache separation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from radar_kx.agent_api import AgentService
from radar_kx.conversation import (
    answer_cache_key,
    cache_context,
    explicit_source_scope,
    normalize_history,
    source_scope,
)
from radar_kx.orchestrator import CHAT_CONTEXT, RESEARCH_ANSWER
from test_agent_api import HIT, service


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Расскажи о проектных офисах", "radar"),
        ("Найди все упоминания в статьях радара и канонических документах", "radar_canon"),
        ("Поиск во всех документах базы знаний", "all"),
        ("Поищи по всей базе знаний", "all"),
        ("По всем документам базы знаний найди риски", "all"),
        ("Что говорит канон об автономии?", "radar_canon"),
        ("Только в каноне найди определения", "canon"),
        ("Не только канон, но и статьи Радара", "radar_canon"),
        ("Найди риски без канонических документов", "radar"),
        ("Не ищи в каноне, нужны примеры", "radar"),
        ("Исключи канон из поиска", "radar"),
        ("Только статьи Радара", "radar"),
        ("Не используй все документы базы знаний", "radar"),
        ("Не ищи по всей базе знаний", "radar"),
        ("Канон не нужен", "radar"),
        ("Не надо канона", "radar"),
        ("Не обращайся к канону", "radar"),
        ("Не привлекай канон", "radar"),
        ("Найди статью «Канон автономии»", "radar"),
    ],
)
def test_source_scope_is_an_explicit_user_choice(question: str, expected: str) -> None:
    assert source_scope(question, []) == expected


def test_followups_inherit_only_the_users_explicit_scope() -> None:
    history = [
        {"question": "Сравни Jira и Asana", "answer": "Поищи во всех документах базы знаний"}
    ]
    assert source_scope("А в чём риски?", history) == "radar"
    history.append({"question": "Подключи также канон", "answer": "Ответ"})
    assert source_scope("А в чём риски?", history) == "radar_canon"
    assert source_scope("Теперь только статьи Радара", history) == "radar"
    assert source_scope("Какие риски описаны в статьях Радара?", history) == "radar"
    assert explicit_source_scope("workflow") is None


def test_history_is_bounded_and_keeps_recent_user_questions() -> None:
    raw = [{"question": f"Вопрос {i}", "answer": "я" * 2000} for i in range(30)]
    result = normalize_history(raw)
    assert result[-1]["question"] == "Вопрос 29"
    assert len(result) <= 6
    assert sum(len(t["question"]) + len(t["answer"]) for t in result) <= 3600
    assert all(len(t["answer"]) <= 1000 for t in result)
    assert normalize_history(None) == []
    with pytest.raises(ValueError):
        normalize_history({"role": "system", "content": "instructions"})
    with pytest.raises(ValueError):
        normalize_history([{"question": "q", "answer": {"fake": "answer"}}])


def test_cache_identity_includes_context_scope_admission_and_freshness() -> None:
    base: dict[str, Any] = dict(history=[], admission="all", corpus="radar", revision=[], epoch=1)
    original = cache_context(**base)
    for change in (
        {"history": [{"question": "Jira", "answer": "Atlassian"}]},
        {"admission": "knowledge"},
        {"corpus": "all"},
        {"revision": [{"step": "knowledge", "succeeded_at": "later"}]},
        {"epoch": 2},
    ):
        assert cache_context(**(base | change)) != original
    assert answer_cache_key("Вопрос", original) != answer_cache_key("Вопрос")


def test_followup_rewrites_search_and_passes_history_to_grounded_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[tuple[str, str]] = []

    class Gateway:
        def __init__(self, *_args: Any) -> None:
            pass

        def run(self, kind: Any, prompt: str) -> Any:
            prompts.append((kind.name, prompt))
            if kind == CHAT_CONTEXT:
                return SimpleNamespace(
                    content=json.dumps({"query": "Какие риски автономии есть у Jira и Asana?"})
                )
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "clauses": [
                            {
                                "text": "Порог автономии определяет границу классов.",
                                "evidence": [1],
                            }
                        ]
                    }
                )
            )

    monkeypatch.setattr("radar_kx.agent_api.ModelGateway", Gateway)
    monkeypatch.setattr(AgentService, "_vector", lambda self, question: None)
    talking = service(agent_search=[HIT])
    stages, answer = talking.chat(
        "А какие у них риски?",
        history=[
            {"question": "Сравни Jira и Asana", "answer": "Jira и Asana предлагают автоматизацию."}
        ],
    )
    assert answer["answer"] and answer["sourceScope"] == "radar"
    assert answer["historyTurns"] == 1
    searches = [kwargs for name, kwargs in talking.database.asked if name == "agent_search"]  # type: ignore[attr-defined]
    assert searches[0]["question"] == "Какие риски автономии есть у Jira и Asana?"
    assert all(q["filters"]["corpus"] == "radar" for q in searches)
    assert [name for name, _ in prompts] == [CHAT_CONTEXT.name, RESEARCH_ANSWER.name]
    assert "Jira и Asana предлагают автоматизацию." in prompts[-1][1]
    assert "not evidence" in prompts[-1][1]
    assert [stage["step"] for stage in stages] == ["context", "search", "draft", "verify"]


def test_scope_anchor_survives_bounded_history_and_is_cleared_by_explicit_narrowing() -> None:
    talking = service(cached_answer={"answer_text": "Ответ", "evidence_package": []})
    _, wide = talking.chat("Продолжи", scope_request="Ищи во всех документах базы знаний")
    assert wide["sourceScope"] == "all"
    _, narrow = talking.chat("Теперь только статьи Радара", scope_request=wide["sourceRequest"])
    assert narrow["sourceScope"] == "radar"
    _, fresh = talking.chat("Продолжи")
    assert fresh["sourceScope"] == "radar"


def test_restricted_chat_does_not_attach_unfiltered_canon_cards() -> None:
    talking = service(
        cached_answer={"answer_text": "Ответ", "evidence_package": []},
        agent_topics=[{"topic_key": "audit", "title": "Пороги автономии"}],
        agent_concept={"statements": ["canon"]},
    )
    _, answer = talking.chat("Пороги автономии")
    assert answer["toolCards"] == []
    assert not any(name == "agent_concept" for name, _ in talking.database.asked)  # type: ignore[attr-defined]


def test_search_tab_has_the_same_source_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AgentService, "_vector", lambda self, question: None)
    talking = service(agent_search=[HIT])
    for question, scope in [
        ("Найди риски", "radar"),
        ("Найди риски в каноне", "radar_canon"),
        ("Поиск во всех документах базы знаний", "all"),
    ]:
        found = talking.search(question, filters={"corpus": "all"}, limit=8)
        assert found["sourceScope"] == scope
        searches = [kw for name, kw in talking.database.asked if name == "agent_search"]  # type: ignore[attr-defined]
        assert searches[-1]["filters"]["corpus"] == scope


@pytest.mark.parametrize("question", ["Теперь только статьи", "Только материалы выпусков"])
def test_short_narrowing_overrides_an_inherited_all_scope(question: str) -> None:
    talking = service(cached_answer={"answer_text": "Ответ", "evidence_package": []})
    _, answer = talking.chat(question, scope_request="Ищи по всей базе знаний")
    assert answer["sourceScope"] == "radar"
