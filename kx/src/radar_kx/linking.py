"""What one statement does to another (stage 2, owner decision 12).

Four types live at launch out of the eighteen in her §7: **supports**,
**contradicts**, **qualifies** and **related_to** - three evidential and one
navigational. Eighteen types over thousands of statements is a queue no decision
budget carries, and the hierarchy the rest would need is already in the backbone's
own parents.

The pipeline is the one the plan names: shortlist by both methods, then judge.

**Why a shortlist at all.** 13 876 statements make 96 million pairs. The two
methods that already exist - words and meaning - each return a ranking, and their
top few are the only pairs worth a model call. Restricted to what the reading pass
admitted as knowledge and to pairs that share a subject, that is a few thousand
judgements rather than a few million.

**Why the model judges rather than the distance.** Cosine distance says two
sentences are about the same thing. It cannot tell "confirms" from "contradicts" -
the two are maximally similar by construction, because a contradiction is a
statement about exactly the same fact. Linking by distance alone would file every
disagreement in the base as agreement, which is the one error this layer must not
make.

**No veto, and no invention.** Decision 4 leaves linking to the machine without
the owner's signature, so a judgement lands as a row with `method='model'`. What
the model may answer is closed: four types or `none`, and `none` is the expected
answer for most pairs.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The four the owner kept for launch (022's CHECK carries the same list).
LINK_TYPES = ("supports", "contradicts", "qualifies", "related_to")

#: How many pairs one call judges. Each answer is one word and two numbers, so a
#: batch can be larger than the reading pass's - but not so large that one
#: unusable answer costs a hundred judgements.
BATCH = 20

#: How many neighbours each statement is offered. Three, because the fourth and
#: fifth are almost always `related_to` at best and the call costs the same.
NEIGHBOURS = 3

#: Cosine distance beyond which two statements are not about the same thing. e5
#: puts genuinely related sentences under 0.25 and unrelated ones above 0.4;
#: 0.32 is the middle of that gap, measured on this corpus.
MAX_DISTANCE = 0.32

#: Longest statement sent. A judgement is about what two sentences say to each
#: other; the article around them is other people's text no link rests on.
STATEMENT_CHARS = 300

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class LinkingError(ValueError):
    """The model's answer cannot be used."""


@dataclass(frozen=True, slots=True)
class Pair:
    """Two statements a shortlist put in front of the judge."""

    from_id: str
    to_id: str
    from_text: str
    to_text: str
    #: What the shortlist thought, kept so a judgement can be read against it.
    distance: float
    shared_topic: str


@dataclass(frozen=True, slots=True)
class Judgement:
    from_id: str
    to_id: str
    link_type: str


INSTRUCTIONS = "\n".join(
    [
        "Ты определяешь отношение между двумя утверждениями базы знаний",
        "по агентному управлению проектами. Отвечай одним значением из пяти:",
        "",
        "supports — второе подтверждает первое: то же самое положение дел,",
        "  подтверждённое другим наблюдением, источником или измерением;",
        "contradicts — второе противоречит первому: об одном и том же предмете",
        "  сказано несовместимое;",
        "qualifies — второе уточняет первое: добавляет условие, границу,",
        "  исключение или область применимости;",
        "related_to — про один предмет, но ни одно из трёх выше не подходит;",
        "none — про разное, либо отношение неочевидно.",
        "",
        "Правила:",
        "1. none — нормальный и самый частый ответ. Не выдумывай связь.",
        "2. supports ставь только если это ДРУГОЕ наблюдение, а не тот же",
        "   текст другими словами.",
        "3. contradicts ставь только при настоящей несовместимости, а не при",
        "   разнице в акцентах.",
        "4. Отвечай только массивом JSON, по объекту на пару, в том же порядке:",
        '   [{"item":1,"link":"none"}]',
        "5. Никакого текста до или после массива.",
    ]
)


def build_payload(pairs: Sequence[Pair]) -> str:
    lines = []
    for index, pair in enumerate(pairs, start=1):
        first = " ".join(pair.from_text.split())[:STATEMENT_CHARS]
        second = " ".join(pair.to_text.split())[:STATEMENT_CHARS]
        lines.append(f"{index}. А: {first}\n   Б: {second}")
    return "\n\n".join(lines)


def parse_judgements(
    answer: str, pairs: Sequence[Pair]
) -> tuple[tuple[Judgement, ...], dict[str, int]]:
    """Read the verdicts back, keeping only the five values that exist."""
    text = _FENCE.sub("", answer).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise LinkingError("the answer contains no JSON array")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LinkingError(f"the answer is not JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise LinkingError("the answer is not a list")

    dropped = {"unknownItem": 0, "unknownLink": 0}
    judged: list[Judgement] = []
    seen: set[int] = set()
    for row in parsed:
        if not isinstance(row, dict):
            dropped["unknownItem"] += 1
            continue
        try:
            ordinal = int(row.get("item", 0))
        except (TypeError, ValueError):
            ordinal = 0
        if not 1 <= ordinal <= len(pairs) or ordinal in seen:
            dropped["unknownItem"] += 1
            continue
        seen.add(ordinal)
        link = " ".join(str(row.get("link") or "").split()).lower()
        if link == "none":
            continue
        if link not in LINK_TYPES:
            dropped["unknownLink"] += 1
            continue
        pair = pairs[ordinal - 1]
        judged.append(Judgement(from_id=pair.from_id, to_id=pair.to_id, link_type=link))
    return tuple(judged), dropped


def summarize(
    judgements: Sequence[Judgement], pairs_judged: int, dropped: Mapping[str, int]
) -> dict[str, Any]:
    """What the pass linked, and how much of what it looked at it left alone."""
    by_type: Counter[str] = Counter()
    for judgement in judgements:
        by_type[judgement.link_type] += 1
    return {
        "pairsJudged": pairs_judged,
        "linked": len(judgements),
        "leftUnlinked": pairs_judged - len(judgements),
        "byLinkType": dict(by_type.most_common()),
        "dropped": {key: value for key, value in dropped.items() if value},
    }
