"""The conversation layer of the agent mode: welcome prompts and tool cards.

Two decisions live here, and both are deliberately cheap:

* The welcome screen's example prompts are **sampled, not fixed**. A pool is
  assembled from a curated core plus concepts the base actually holds, so the
  examples follow the base rather than a hardcoded list going stale.
* Tool selection is **deterministic**. The question is matched against topic
  titles and a small keyword set; whatever does not match runs the verified
  evidence pipeline, which is the correct default for every question. A model
  classifier would spend a call to be wrong in new ways; this costs nothing and
  is testable line by line. When a question names a subject, the reader gets
  that subject's card beside the answer - the card is data from the base, not a
  model claim, so it carries no verification and needs none.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PROMPT_CATEGORIES: tuple[str, ...] = ("find", "concept", "contra", "watch")
PROMPTS_ON_WELCOME = 6
PER_CATEGORY_ON_WELCOME = 2
#: Topics need this many statements before inviting a question about them: a
#: subject with two claims is a card, not a conversation.
MIN_STATEMENTS_FOR_PROMPT = 5
_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class CuratedPrompt:
    category: str
    hint: str
    text: str


#: The curated core: question shapes the base answers well, written to carry
#: their own trigger words, so a prompt from the pool selects its own tool.
CURATED_PROMPTS: tuple[CuratedPrompt, ...] = (
    CuratedPrompt(
        "find",
        "поиск с доказательствами",
        "Чем подтверждённые положения отличаются от наблюдаемых сигналов?",
    ),
    CuratedPrompt(
        "find",
        "поиск с доказательствами",
        "Что база говорит о governed autonomy в распределённых командах?",
    ),
    CuratedPrompt(
        "find",
        "поиск с доказательствами",
        "Какие требования к аудиту действий агентов зафиксированы в каноне?",
    ),
    CuratedPrompt(
        "find",
        "поиск с доказательствами",
        "Кто в организации отвечает за ошибку, допущенную агентом?",
    ),
    CuratedPrompt(
        "find", "поиск с доказательствами", "Что считается первоисточником, а что пересказом?"
    ),
    CuratedPrompt(
        "find", "поиск с доказательствами", "Какие метрики внедрения агентов упоминают команды?"
    ),
    CuratedPrompt("concept", "карточка понятия", "Какие статусы бывают у утверждений?"),
    CuratedPrompt("concept", "карточка понятия", "Что входит в понятие «канон»?"),
    CuratedPrompt(
        "contra", "противоречия по теме", "Где база видит противоречия в оценках эффекта агентов?"
    ),
    CuratedPrompt(
        "contra", "противоречия по теме", "Есть ли разногласия о границах автономии агентов?"
    ),
    CuratedPrompt(
        "contra", "противоречия по теме", "Что противоречиво в подходах к human-in-the-loop?"
    ),
    CuratedPrompt(
        "watch",
        "честный отказ + ближайшее",
        "Сколько организаций внедрило агентов в продакшне в 2025 году?",
    ),
    CuratedPrompt(
        "watch", "честный отказ + ближайшее", "Какая доля PMO использует агентов по данным опросов?"
    ),
    CuratedPrompt(
        "watch", "честный отказ + ближайшее", "Динамика внедрения за 12 месяцев - есть ли тренд?"
    ),
)

_CONTRA_WORDS = ("противореч", "разноглас", "спор о", "несоглас")
_WATCH_WORDS = ("сколько организаций", "какая доля", "рынке", "топ-", "тренд", "динамика внедрения")
_GAP_WORDS = ("пробел", "чего не хватает", "не покрывает")

#: A topic title must be this long before it is trusted as a subject match:
#: shorter strings match as substrings of unrelated words.
_MIN_TOPIC_MATCH = 6


def valid_session(session: str) -> bool:
    """A session id is a client-chosen label, not a permission.

    It travels back to the client untouched and is not written anywhere: the
    owner's decision is that questions and answers are stored for analysis
    without addresses, and binding them to a session is a schema change that
    has not been asked for.
    """
    return not session or _SESSION_PATTERN.fullmatch(session) is not None


def welcome_prompts(
    topics: Sequence[Mapping[str, Any]], *, count: int = PROMPTS_ON_WELCOME, seed: int | None = None
) -> dict[str, Any]:
    """Assemble the pool, then sample the welcome screen from it.

    Deterministic under a fixed `seed`: a test (and a curious owner) must be
    able to see exactly what a given session would have been offered.
    """
    pool: list[CuratedPrompt] = list(CURATED_PROMPTS)
    for topic in topics:
        title = str(topic.get("title") or "").strip()
        try:
            statements = int(topic.get("statements") or 0)
        except (TypeError, ValueError):
            statements = 0
        if statements < MIN_STATEMENTS_FOR_PROMPT or len(title) < _MIN_TOPIC_MATCH:
            continue
        pool.append(CuratedPrompt("concept", "карточка понятия", f"Расскажи про «{title}»"))
    # Sampling a welcome screen, not a key: `random` is the right tool (S311).
    generator = random.Random(seed)  # noqa: S311
    picked: list[CuratedPrompt] = []
    per_category: dict[str, int] = {}
    shuffled = pool[:]
    generator.shuffle(shuffled)
    for prompt in shuffled:
        if len(picked) >= min(count, len(shuffled)):
            break
        used = per_category.get(prompt.category, 0)
        if used >= PER_CATEGORY_ON_WELCOME:
            continue
        per_category[prompt.category] = used + 1
        picked.append(prompt)
    return {
        "prompts": [
            {"text": prompt.text, "category": prompt.category, "hint": prompt.hint}
            for prompt in picked
        ],
        "pool": len(pool),
        "poolCurated": len(CURATED_PROMPTS),
    }


@dataclass(frozen=True, slots=True)
class ToolChoice:
    """What the agent will show beside the answer, and how it was chosen."""

    tool: str
    topic_key: str | None = None
    because: str = "default"


#: The tool names the chat response carries. `find` is the evidence pipeline
#: itself and adds no card: the answer's evidence already is the card.
TOOL_FIND = "find"
TOOL_CONCEPT = "concept"
TOOL_CONTRA = "contra"
TOOL_GAPS = "gaps"
TOOL_WATCH = "watch"


def select_tool(question: str, topics: Sequence[Mapping[str, Any]]) -> ToolChoice:
    """Pick a card deterministically. Unmatched questions stay on `find`.

    Order matters and is the cheap-to-expensive one: a named subject beats a
    keyword, because a question that names a subject wants that subject, even
    if it also says «противоречия».
    """
    lowered = question.lower()
    for topic in topics:
        title = str(topic.get("title") or "").strip().lower()
        topic_key = str(topic.get("topic_key") or "").strip()
        if len(title) >= _MIN_TOPIC_MATCH and title in lowered and topic_key:
            return ToolChoice(TOOL_CONCEPT, topic_key=topic_key, because="topic")
    if any(word in lowered for word in _CONTRA_WORDS):
        return ToolChoice(TOOL_CONTRA, because="keyword")
    if any(word in lowered for word in _GAP_WORDS):
        return ToolChoice(TOOL_GAPS, because="keyword")
    if any(word in lowered for word in _WATCH_WORDS):
        return ToolChoice(TOOL_WATCH, because="keyword")
    return ToolChoice(TOOL_FIND)


def tool_card_limit(question: str) -> int:
    """How many rows a card carries. A card is context, not a feed."""
    return 5
