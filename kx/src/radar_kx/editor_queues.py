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


def _decide_skeleton(database: Database, item_id: str, action: str, actor: str) -> dict[str, Any]:
    return database.decide_skeleton(source=item_id, verdict=action, actor=actor)


QUEUES: tuple[Queue, ...] = (
    Queue(
        key="skeleton",
        title="Скелет тем",
        why=(
            "На чём строится база знаний. Сегодня — ни на чём: утверждения "
            "сопоставлялись с цитатами по общим словам, и ничто не требовало, чтобы "
            "совпадение было про один предмет. Именно отсюда посредственная "
            "связанность.\n\nСкелет при этом существует — дважды, прозой, на двух "
            "страницах wiki, которые не вполне согласны между собой, и ни один из них "
            "не лежит в хранилище. Выберите тот, что берём за хребет: его элементы "
            "станут таблицей тем, по которой пойдёт разметка материалов и ограничение "
            "привязок."
        ),
        load=_load_skeleton,
        decide=_decide_skeleton,
        object_kind="topic_skeleton",
        empty="Скелет принят.",
    ),
    Queue(
        key="evidence",
        title="Привязки утверждений к доказательствам",
        why=(
            "Машина нашла в хранилище цитаты, похожие на то, что говорит страница wiki. "
            "Похоже — не значит «на этом стоит»: решение о том, чем подкреплено "
            "утверждение, редакторское. Подтверждённая привязка попадает в граф и в "
            "публикуемый срез; неподтверждённая не попадает никуда."
        ),
        load=_load_evidence,
        decide=_decide_evidence,
        object_kind="concept_evidence",
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
    Queue(
        key="aliases",
        title="Написания имён",
        why=(
            "Перевод не донёс имя собственное в зарегистрированной форме. Цитату это "
            "не блокирует: имя показывается в оригинале, а предложение ждёт здесь без "
            "срока."
        ),
        load=_load_aliases,
        decide=_decide_alias,
        object_kind="entity_alias",
    ),
)

QUEUES_BY_KEY = {queue.key: queue for queue in QUEUES}


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
    if action not in {"confirmed", "rejected"}:
        raise ValueError("action must be confirmed or rejected")
    return queue.decide(database, item_id, action, actor)


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
