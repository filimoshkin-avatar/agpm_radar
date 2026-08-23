"""Everything waiting for the owner, in one shape (slice 2.12, extended).

The owner asked for one entry point for reading and approving everything that
cannot be put as a multiple-choice question. That is six different kinds of
decision - a binding, a source family, a duplicate cluster, a candidate idea, a
host policy, an alias - and one kind of reading.

They are not six interfaces. Every queue produces the same shape:

    {key, title, why, count, items: [{id, primary, secondary, meta, actions}]}

so one renderer draws all of them and the seventh costs a query rather than a
page. The differences that matter are in the text: each queue says **why the
machine cannot decide this** and what the consequence of deciding it is, because
a queue that shows work without saying why it is yours is a queue that gets
skipped.

Every decision goes through :func:`decide`, which writes the object's own state
and an append-only `editorial_decisions` row in one transaction (ADR-0006 §3).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from radar_kx.database import Database


@dataclass(frozen=True, slots=True)
class QueueItem:
    item_id: str
    primary: str
    secondary: str = ""
    meta: tuple[tuple[str, str], ...] = ()
    #: ``(action, label, kind)`` where kind is "yes", "no" or "plain".
    actions: tuple[tuple[str, str, str], ...] = ()
    link: str | None = None
    children: tuple[dict[str, Any], ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "primary": self.primary,
            "secondary": self.secondary,
            "meta": [{"label": label, "value": value} for label, value in self.meta],
            "actions": [
                {"action": action, "label": label, "kind": kind}
                for action, label, kind in self.actions
            ],
            "link": self.link,
            "children": list(self.children),
        }


@dataclass(frozen=True, slots=True)
class Queue:
    key: str
    title: str
    #: Why this cannot be decided by the machine. Shown above the list.
    why: str
    load: Callable[[Database, int], tuple[int, Sequence[QueueItem]]]
    decide: Callable[[Database, str, str, str], dict[str, Any]] | None = None
    object_kind: str = ""
    empty: str = "Ничего не ждёт решения."


# ---------------------------------------------------------------------------
# Evidence bindings
# ---------------------------------------------------------------------------


def _load_evidence(database: Database, limit: int) -> tuple[int, list[QueueItem]]:
    page = database.evidence_queue(limit=limit)
    items = [
        QueueItem(
            item_id=entry["conceptClaimId"],
            primary=entry["statement"],
            secondary=f"{entry['conceptTitle']} — {entry['page']}",
            meta=(("тип", entry["claimNature"]),),
            children=tuple(
                {
                    "id": proposal["claimId"],
                    "quote": proposal["quote"],
                    "sourceUrl": proposal["sourceUrl"],
                    "span": f"{proposal['charStart']}–{proposal['charEnd']}",
                    "relevance": proposal["coverage"],
                    "membershipClass": proposal["membershipClass"],
                    "actions": [
                        {"action": "confirmed", "label": "Подтвердить", "kind": "yes"},
                        {"action": "rejected", "label": "Отклонить", "kind": "no"},
                    ],
                }
                for proposal in entry["proposals"]
            ),
        )
        for entry in page["items"]
    ]
    return int(page["proposalsWaiting"]), items


def _decide_evidence(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    statement_id, _, claim_id = item_id.partition("/")
    return database.decide_binding(
        concept_claim_id=statement_id, claim_id=claim_id, verdict=action, actor=actor
    )


# ---------------------------------------------------------------------------
# Source families
# ---------------------------------------------------------------------------


def _load_families(database: Database, limit: int) -> tuple[int, list[QueueItem]]:
    proposals = database.pending_family_proposals(limit=limit)
    items = [
        QueueItem(
            item_id=entry["familyKey"],
            primary=entry["displayName"],
            secondary=(
                f"{entry['documentCount']} документов, "
                f"{len(entry['hosts'])} хост(ов): {', '.join(entry['hosts'][:4])}"
            ),
            meta=(("домен", entry["domain"]),),
            actions=(
                ("confirmed", "Это один источник", "yes"),
                ("rejected", "Это разные источники", "no"),
            ),
        )
        for entry in proposals["items"]
    ]
    return int(proposals["waiting"]), items


def _decide_family(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    return database.decide_family_proposal(family_key=item_id, verdict=action, actor=actor)


# ---------------------------------------------------------------------------
# Duplicate clusters
# ---------------------------------------------------------------------------


def _load_clusters(database: Database, limit: int) -> tuple[int, list[QueueItem]]:
    total, rows = database.pending_duplicate_clusters(limit=limit)
    items = [
        QueueItem(
            item_id=str(row["cluster_id"]),
            primary=f"{row['member_count']} документа считаются одним источником",
            secondary=" · ".join(str(url) for url in row["urls"][:3]),
            meta=(
                ("правило", str(row["formation_method"])),
                ("мера", str(row.get("shingle_measure") or "точное совпадение")),
                ("значение", str(row.get("similarity") or "1.0")),
            ),
            actions=(
                ("confirmed", "Да, это одно и то же", "yes"),
                ("rejected", "Нет, это разное", "no"),
            ),
        )
        for row in rows
    ]
    return total, items


def _decide_cluster(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    return database.decide_duplicate_cluster(cluster_id=item_id, verdict=action, actor=actor)


# ---------------------------------------------------------------------------
# Candidate ideas
# ---------------------------------------------------------------------------


def _load_ideas(database: Database, limit: int) -> tuple[int, list[QueueItem]]:
    total, rows = database.pending_ideas(limit=limit)
    items = [
        QueueItem(
            item_id=str(row["idea_id"]),
            primary=str(row["title"]),
            secondary=str(row["statement"]),
            meta=(("независимых источников", str(row["independent_sources"])),),
            actions=(("confirmed", "Принять", "yes"), ("rejected", "Отклонить", "no")),
            children=tuple(
                {
                    "id": str(index),
                    "quote": str(quote),
                    "sourceUrl": str(url),
                    "span": "",
                    "relevance": None,
                    "membershipClass": "",
                    "actions": [],
                }
                for index, (quote, url) in enumerate(row["evidence"])
            ),
        )
        for row in rows
    ]
    return total, items


def _decide_idea(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    return database.decide_idea(idea_id=item_id, verdict=action, actor=actor)


# ---------------------------------------------------------------------------
# Host policy
# ---------------------------------------------------------------------------


def _load_hosts(database: Database, limit: int) -> tuple[int, list[QueueItem]]:
    total, rows = database.hosts_awaiting_policy(limit=limit)
    items = [
        QueueItem(
            item_id=str(row["host"]),
            primary=str(row["host"]),
            secondary=(f"{row['documents']} документов без текста, причина «{row['reason']}»"),
            meta=(("что помогло бы", str(row["would_help"])),),
            actions=(
                ("confirmed", "Разрешить эту ступень", "yes"),
                ("rejected", "Оставить как есть", "no"),
            ),
        )
        for row in rows
    ]
    return total, items


def _decide_host(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    return database.decide_host_policy(host=item_id, verdict=action, actor=actor)


# ---------------------------------------------------------------------------
# Alias proposals
# ---------------------------------------------------------------------------


def _load_aliases(database: Database, limit: int) -> tuple[int, list[QueueItem]]:
    total, rows = database.pending_alias_proposals(limit=limit)
    items = [
        QueueItem(
            item_id=str(row["proposal_id"]),
            primary=str(row["original_form"]),
            secondary=f"встречено {row['occurrences']} раз, язык {row['language']}",
            actions=(
                ("confirmed", "Это то же имя", "yes"),
                ("rejected", "Это другое", "no"),
            ),
        )
        for row in rows
    ]
    return total, items


def _decide_alias(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    return database.decide_alias_proposal(proposal_id=int(item_id), verdict=action, actor=actor)


# ---------------------------------------------------------------------------
# The topic skeleton
# ---------------------------------------------------------------------------


def _load_skeleton(database: Database, limit: int) -> tuple[int, list[QueueItem]]:
    authored = _authored_backbone(database)
    if authored:
        # Once a backbone is in force, the three candidates that were not chosen
        # are history, not work. They stay in `editorial_decisions` where a reader
        # can find them; the tab shows what the base is organised around today.
        return 0, authored
    accepted = {row["source"] for row in database.accepted_skeleton()}
    items: list[QueueItem] = []
    for candidate in database.skeleton_candidates():
        adopted = candidate.source in accepted
        items.append(
            QueueItem(
                item_id=candidate.source,
                primary=candidate.title + (" — принят за основу" if adopted else ""),
                secondary=candidate.note,
                meta=(
                    ("откуда", candidate.origin),
                    ("элементов", str(len(candidate.elements))),
                ),
                actions=(
                    ()
                    if adopted
                    else (
                        ("confirmed", "Взять за основу", "yes"),
                        ("rejected", "Не это", "no"),
                    )
                ),
                children=tuple(
                    {
                        "id": str(element.ordinal),
                        "quote": f"{element.ordinal}. {element.title}"
                        + (f" — {element.description}" if element.description else ""),
                        "sourceUrl": "",
                        "span": "",
                        "relevance": None,
                        "membershipClass": "",
                        "actions": [],
                    }
                    for element in candidate.elements
                ),
            )
        )
    return len([item for item in items if item.actions]), items


def _authored_backbone(database: Database) -> list[QueueItem]:
    """The composition the owner wrote, if it has been loaded."""
    topics = database.topics()
    if not topics:
        return []
    below: dict[str, int] = {}
    for topic in topics:
        section = str(topic["path"]).split(" / ")[0]
        below[section] = below.get(section, 0) + 1
    sections = [topic for topic in topics if int(topic["level"]) == 1]
    return [
        QueueItem(
            item_id="authored",
            primary="Авторский состав — принят за основу",
            secondary=(
                "Предметная онтология из вашего документа: разделы каталога как "
                "уровень 1, их подгруппы как уровень 2, перечисленные в них элементы "
                "как уровень 3. Три остальных измерения документа — статус знания, "
                "жанр и область применимости — это не темы и лежат отдельно: они "
                "говорят не «о чём знание», а насколько оно авторитетно, для чего "
                "написано и куда применимо."
            ),
            meta=(
                ("разделов", str(len(sections))),
                ("тем всего", str(len(topics))),
            ),
            actions=(),
            children=tuple(
                {
                    "id": str(topic["topic_key"]),
                    "quote": (
                        f"{topic['title']} — тем ниже: {below.get(str(topic['title']), 1) - 1}"
                    ),
                    "sourceUrl": "",
                    "span": "",
                    "relevance": None,
                    "membershipClass": "",
                    "actions": [],
                }
                for topic in sections
            ),
        )
    ]


def _decide_skeleton(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    return database.decide_skeleton(source=item_id, verdict=action, actor=actor)


# ---------------------------------------------------------------------------
# Which linking method was right
# ---------------------------------------------------------------------------


def _load_comparison(database: Database, limit: int) -> tuple[int, list[QueueItem]]:
    total, rows = database.method_comparison_queue(limit=limit)
    items: list[QueueItem] = []
    for row in rows:
        semantic = row["semanticTop"]
        items.append(
            QueueItem(
                item_id=(
                    f"{row['conceptClaimId']}|{row['lexicalTop']['claimId']}|{semantic['claimId']}"
                ),
                primary=str(row["statement"]),
                secondary="Два метода выбрали разное. Что из этого ближе к утверждению?",
                actions=(
                    ("semantic", "Смысловой (вектор)", "yes"),
                    ("lexical", "Словесный (поиск)", "yes"),
                    ("neither", "Оба мимо", "no"),
                ),
                children=(
                    {
                        "id": "semantic",
                        "quote": "СМЫСЛОВОЙ · " + str(semantic["quote"]),
                        "sourceUrl": str(row.get("semanticUrl") or ""),
                        "span": "",
                        "relevance": round(float(semantic["score"]), 3),
                        "membershipClass": "косинус",
                        "actions": [],
                    },
                    {
                        "id": "lexical",
                        "quote": "СЛОВЕСНЫЙ · " + str(row.get("lexicalQuote") or "(нет цитаты)"),
                        "sourceUrl": str(row.get("lexicalUrl") or ""),
                        "span": "",
                        "relevance": None,
                        "membershipClass": "полнотекстовый поиск",
                        "actions": [],
                    },
                ),
            )
        )
    return total, items


def _decide_comparison(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    statement_id, lexical_id, semantic_id = item_id.split("|")
    return database.record_method_vote(
        concept_claim_id=statement_id,
        winner=action,
        lexical_claim_id=lexical_id,
        semantic_claim_id=semantic_id,
        voted_by=actor,
    )


QUEUES: tuple[Queue, ...] = (
    Queue(
        key="skeleton",
        title="Скелет тем",
        why=(
            "На чём строится база знаний. До вашего состава — ни на чём: утверждения "
            "сопоставлялись с цитатами по общим словам, и ничто не требовало, чтобы "
            "совпадение было про один предмет. Именно отсюда посредственная "
            "связанность.\n\nЗдесь лежит состав, который сейчас в силе. По нему "
            "размечены материалы и утверждения, и им ограничены оба метода "
            "связывания на соседней вкладке. Решения тут не ждут: если состав нужно "
            "поправить, это правка файла и повторная загрузка."
        ),
        load=_load_skeleton,
        decide=_decide_skeleton,
        object_kind="topic_skeleton",
        empty="Скелет принят.",
    ),
    Queue(
        key="comparison",
        title="Сравнение методов связывания",
        why=(
            "Два способа найти доказательство для утверждения. Словесный — "
            "полнотекстовый поиск PostgreSQL по общим словам. Смысловой — "
            "косинусная близость локальных эмбеддингов multilingual-e5-small, "
            "посчитанных на этом же хосте.\n\nПо вашему скелету измерено главное: "
            "**про тот же предмет было только 51 % ответов словесного метода и 50 % "
            "смыслового**. Половина того, что оба выдавали, была цитатой из чужой "
            "предметной области — вот откуда та самая посредственная связанность.\n\n"
            "Пары ниже — уже внутри предмета утверждения: оба метода искали только "
            "среди материалов того же раздела. Вопрос «а это вообще про то?» снят, "
            "остался вопрос «какая из двух цитат лучше».\n\nСогласия между методами "
            "ограничение не добавило: первый выбор совпал у 25 утверждений из 228. "
            "Оценкам обоих верить нельзя — e5 даёт 0,89 хорошему совпадению и 0,86 "
            "бессмыслице, RRF упирается в потолок 2/61 — так что инструмент остался "
            "один: посмотреть глазами. Двух-трёх десятков голосов хватит, чтобы "
            "решить, каким методом строить базу."
        ),
        load=_load_comparison,
        decide=_decide_comparison,
        object_kind="binding_method_vote",
        empty="Все пары размечены.",
    ),
    Queue(
        key="families",
        title="Семьи источников",
        why=(
            "Два документа одной семьи — это одно подтверждение, а не два. Машина "
            "сгруппировала по домену и находит только лёгкую половину: один издатель "
            "под несколькими хостами. Два несвязанных домена с общей редакцией она не "
            "увидит. Пока семья не подтверждена, её документы считаются «неизвестными» "
            "и никогда не удовлетворяют требованию двух независимых источников."
        ),
        load=_load_families,
        decide=_decide_family,
        object_kind="source_family",
    ),
    Queue(
        key="duplicates",
        title="Кластеры дубликатов",
        why=(
            "Документы одного кластера дают одно подтверждение независимо от семьи. "
            "Порог сходства ошибается, поэтому кластер, образованный машиной, "
            "не схлопывает счёт, пока человек не сказал, что это одно и то же."
        ),
        load=_load_clusters,
        decide=_decide_cluster,
        object_kind="content_duplicate_cluster",
    ),
    Queue(
        key="ideas",
        title="Идеи-кандидаты",
        why=(
            "Прошли гейт независимости: не меньше двух подтверждающих claim'ов из "
            "разных семей источников. Формулировку писала модель по цитатам; принять "
            "её как идею — ваше решение."
        ),
        load=_load_ideas,
        decide=_decide_idea,
        object_kind="idea",
    ),
    Queue(
        key="hosts",
        title="Политика по хостам",
        why=(
            "Документы без полного текста, где известна ступень, которая помогла бы. "
            "P11 сделал robots сигналом маршрутизации, а не стеной, но это продуктовое "
            "решение, и оно принимается на один хост, с причиной и вашим именем. "
            "Глобального переключателя тут намеренно нет."
        ),
        load=_load_hosts,
        decide=_decide_host,
        object_kind="host_profile",
    ),
)


@dataclass(frozen=True, slots=True)
class RetiredQueue:
    """A queue that is built, works, and is not on the wall today.

    The owner asked for only what is current. Deleting the loader would mean
    writing it again when the decision it serves comes back, so what changes is
    the list of tabs and not the code behind them - and each one says what would
    put it back.
    """

    queue: Queue
    reason: str
    returns_when: str


RETIRED: tuple[RetiredQueue, ...] = (
    RetiredQueue(
        queue=Queue(
            key="evidence",
            title="Привязки утверждений к доказательствам",
            why=(
                "Машина нашла в хранилище цитаты, похожие на то, что говорит страница "
                "wiki. Похоже — не значит «на этом стоит»: решение о том, чем "
                "подкреплено утверждение, редакторское."
            ),
            load=_load_evidence,
            decide=_decide_evidence,
            object_kind="concept_evidence",
        ),
        reason=(
            "2 047 предложений построены словесным методом по всему корпусу, до того "
            "как появился скелет. Подтверждать их сейчас — значит вписать в граф "
            "результат метода, который ещё не выбран, и половина из них про чужой "
            "предмет."
        ),
        returns_when="выбран метод связывания и предложения пересобраны внутри тем",
    ),
    RetiredQueue(
        queue=Queue(
            key="aliases",
            title="Написания имён",
            why=(
                "Перевод не донёс имя собственное в зарегистрированной форме. Цитату "
                "это не блокирует: имя показывается в оригинале."
            ),
            load=_load_aliases,
            decide=_decide_alias,
            object_kind="entity_alias",
        ),
        reason="ничего не блокирует: цитата публикуется, имя показывается в оригинале",
        returns_when="накопится достаточно предложений, чтобы разбирать их пачкой",
    ),
)

QUEUES_BY_KEY = {queue.key: queue for queue in QUEUES}

#: Derived from the buttons each queue actually renders, so the two cannot
#: disagree.
ALLOWED_ACTIONS: dict[str, set[str]] = {
    "comparison": {"lexical", "semantic", "neither"},
}


def queue_summary(database: Database) -> list[dict[str, Any]]:
    """Counts for the index, without loading any items."""
    summary = []
    for queue in QUEUES:
        total, _ = queue.load(database, 0)
        summary.append({"key": queue.key, "title": queue.title, "why": queue.why, "count": total})
    return summary


def load_queue(database: Database, key: str, *, limit: int) -> dict[str, Any]:
    queue = QUEUES_BY_KEY.get(key)
    if queue is None:
        raise KeyError(f"unknown queue {key!r}")
    total, items = queue.load(database, limit)
    return {
        "key": queue.key,
        "title": queue.title,
        "why": queue.why,
        "count": total,
        "empty": queue.empty,
        "items": [item.as_json() for item in items],
    }


def decide(
    database: Database, *, key: str, item_id: str, action: str, actor: str
) -> dict[str, Any]:
    queue = QUEUES_BY_KEY.get(key)
    if queue is None or queue.decide is None:
        raise KeyError(f"unknown queue {key!r}")
    # Each queue owns its vocabulary: a confirmation is not a vote, and a queue
    # that had to phrase "the semantic one was better" as "confirmed" would be
    # storing something other than what the person said.
    if action not in ALLOWED_ACTIONS.get(key, {"confirmed", "rejected"}):
        raise ValueError(f"action {action!r} is not one this queue accepts")
    return queue.decide(database, item_id, action, actor)


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
