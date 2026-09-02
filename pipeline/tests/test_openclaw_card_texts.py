"""Card text rules of the OpenClaw layer: bound to the article, no templates, no repeats.

Run from the repository root with the V2 environment, which carries pytest:
    v2/.venv/bin/python -m pytest pipeline/tests -q
"""

from __future__ import annotations

import pytest

import agpm_radar_openclaw_analysis as m

ARTICLE = (
    "Atlassian is turning Jira into the place where AI work gets assigned, tracked and reviewed. "
    "Ming Wu, head of engineering for DevAI, said Rovo agents will pick up tickets, open pull "
    "requests and report back into the issue. The rollout starts with 40 enterprise customers "
    "in October 2026."
)
TITLE = "Atlassian extends AI reach of Jira into agentic engineering workflows"
FACTUAL = (
    "Atlassian встраивает в Jira агентов Rovo: они берут задачи, открывают запросы на слияние и "
    "отчитываются в задаче. По словам Ming Wu, пилот начнётся с 40 корпоративных клиентов "
    "в октябре 2026 года."
)
ANGLE = (
    "Для PMO это повод заранее решить, кто подтверждает результат агента в тикете и как журнал "
    "его действий попадает в отчётность проекта. Риск в том, что статус задачи станет менять "
    "не человек."
)


def card(short_text: str = FACTUAL, agpm_angle: str = ANGLE) -> dict[str, str]:
    return {"short_text": short_text, "agpm_angle": agpm_angle}


def test_factual_card_passes() -> None:
    m.validate_card_text(card(), source_text=ARTICLE, title=TITLE)


def test_description_that_only_reworks_the_title_is_rejected() -> None:
    generic = (
        "Atlassian расширяет возможности Jira в сторону агентных инженерных процессов, "
        "и это меняет работу команд разработки в компании."
    )
    with pytest.raises(RuntimeError, match="names nothing from the article body"):
        m.validate_card_text(card(short_text=generic), source_text=ARTICLE, title=TITLE)


def test_template_phrases_are_rejected_in_either_field() -> None:
    template = (
        "Материал «Atlassian» описывает переход от отдельных AI-помощников к агентным workflow: "
        "агенты получают роль в координации задач, как в Jira с агентами Rovo."
    )
    with pytest.raises(RuntimeError, match="template phrase"):
        m.validate_card_text(card(short_text=template), source_text=ARTICLE, title=TITLE)
    governance = (
        "Для AgPM материал усиливает governance-линию: агентная система должна быть управляемой, "
        "наблюдаемой и ограниченной правилами и журналом действий Rovo."
    )
    with pytest.raises(RuntimeError, match="template phrase"):
        m.validate_card_text(card(agpm_angle=governance), source_text=ARTICLE, title=TITLE)


def test_short_field_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="too short"):
        m.validate_card_text(card(agpm_angle="Коротко про Rovo."), source_text=ARTICLE, title=TITLE)


def test_angle_that_copies_the_description_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="repeats short_text"):
        m.validate_card_text(card(agpm_angle=FACTUAL), source_text=ARTICLE, title=TITLE)


def test_repeated_pair_is_found_by_opening_and_by_wording() -> None:
    lead = "Материал показывает переход от отдельных помощников к агентным процессам"
    a = {
        "id": "a",
        "short_text": f"{lead}, где контролируются расходы.",
        "agpm_angle": "Первый вывод про бюджет и лимиты действий агента в проекте.",
    }
    b = {
        "id": "b",
        "short_text": f"{lead}, где координируются операции.",
        "agpm_angle": "Второй вывод про портфель и точки подтверждения эскалаций.",
    }
    found = m.repeated_card_pair([a, b])
    assert found is not None and found[:3] == ("short_text", "a", "b")
    c = {**b, "id": "c", "short_text": FACTUAL}
    d = {**a, "id": "d", "short_text": "Кроме того, " + FACTUAL[0].lower() + FACTUAL[1:]}
    found = m.repeated_card_pair([c, d])
    assert found is not None and found[:3] == ("short_text", "c", "d")
    assert m.repeated_card_pair([a, c]) is None


def test_normalize_card_accepts_an_object_or_a_singleton_list() -> None:
    assert m.normalize_card([{"short_text": " a ", "agpm_angle": "b"}]) == {
        "short_text": "a",
        "agpm_angle": "b",
    }
    with pytest.raises(RuntimeError, match="lacks"):
        m.normalize_card({"short_text": "a"})
    with pytest.raises(RuntimeError, match="not an object"):
        m.normalize_card("text")


def test_prompt_carries_the_article_and_the_rejection_but_not_the_template_summary() -> None:
    material = {
        "id": "m1",
        "title": TITLE,
        "url": "https://example.test/a",
        "source_name": "devops.com",
        "summary": "Материал «Atlassian» описывает переход от отдельных AI-помощников к агентным workflow",
        "agpm_takeaway": "Для AgPM материал усиливает governance-линию",
        "rubrics": ["governance_control"],
    }
    prompt = m.card_prompt(material, ARTICLE, [])
    assert ARTICLE in prompt
    assert "governance и контроль агентов" in prompt
    assert "описывает переход" not in prompt
    assert "усиливает governance-линию" not in prompt.split("Статья:")[1]
    assert "Предыдущий ответ" not in prompt
    repaired = m.card_prompt(material, ARTICLE, ["short_text is too short: 20 chars"])
    assert "Предыдущий ответ отклонён: short_text is too short: 20 chars" in repaired


def test_overlong_field_is_rejected() -> None:
    long_angle = (ANGLE + " ") * 4
    assert len(long_angle) > m.CARD_MAX_TEXT_CHARS["agpm_angle"]
    with pytest.raises(RuntimeError, match="too long"):
        m.validate_card_text(card(agpm_angle=long_angle), source_text=ARTICLE, title=TITLE)


def test_runglish_is_rejected_and_names_pass() -> None:
    runglish = (
        "Salesforce в Winter ’27 заявила, что агенты будут выполнять end-to-end workflows в CRM, "
        "Slack, Microsoft Teams, голосе и legacy-системах: booking calls «до секунд», Agentic "
        "Commerce Search даёт 13% lift. NIST запустил AI Agent Standards Initiative с фокусом на "
        "AI agent security, identity и authorization; Cloudflare оценивает non-human traffic."
    )
    assert m.card_foreign_words(runglish) == [
        "end-to-end",
        "workflows",
        "legacy",
        "booking",
        "calls",
        "lift",
        "agent",
        "security",
        "identity",
        "authorization",
        "non-human",
        "traffic",
    ]
    russian = (
        "Salesforce в выпуске Winter ’27 обещает сквозные рабочие процессы в CRM, Slack и "
        "Microsoft Teams; NIST запустил инициативу AI Agent Standards Initiative, а Center for AI "
        "Safety и портал DevOps.com пишут об iPhone, GPT-5.5 и AgPM без англицизмов."
    )
    assert m.card_foreign_words(russian) == []


def test_card_with_runglish_is_rejected_with_the_words_named() -> None:
    angle = ANGLE.replace("журнал его действий", "audit log его действий")
    with pytest.raises(RuntimeError, match="English words outside proper names.*audit, log"):
        m.validate_card_text(card(agpm_angle=angle), source_text=ARTICLE, title=TITLE)


def test_prompt_states_the_language_rule() -> None:
    material = {"id": "m1", "title": TITLE, "url": "https://example.test/a", "rubrics": []}
    prompt = m.card_prompt(material, ARTICLE, [])
    assert "целиком по-русски" in prompt
    assert "сквозные рабочие процессы" in prompt


def test_lowercase_brand_spelled_as_a_domain_in_the_source_is_allowed() -> None:
    source = "monday.com launches Sidekick, an agent that drafts boards. Read more at monday.com/blog."
    assert m.card_brand_words("monday.com adds agents", source) == frozenset({"monday"})
    text = "monday выпустила Sidekick: агент собирает доски и переносит задачи в рабочие процессы."
    assert m.card_foreign_words(text, m.card_brand_words(source)) == []
    assert m.card_foreign_words(text) == ["monday"]


def test_brand_of_the_source_address_is_allowed() -> None:
    text = FACTUAL.replace("Atlassian встраивает", "monday встраивает")
    with pytest.raises(RuntimeError, match="write them in Russian: monday"):
        m.validate_card_text(card(short_text=text), source_text=ARTICLE, title=TITLE)
    m.validate_card_text(
        card(short_text=text),
        source_text=ARTICLE,
        title=TITLE,
        url="https://monday.com/blog/ai-workspace/",
    )


def test_product_named_right_after_the_brand_is_allowed() -> None:
    allowed = frozenset({"monday"})
    text = "monday sidekick собирает доски, а monday vibe строит приложения; agents тут англицизм."
    assert m.card_foreign_words(text, allowed) == ["agents"]
    assert m.card_foreign_words("sidekick без бренда рядом", allowed) == ["sidekick"]
