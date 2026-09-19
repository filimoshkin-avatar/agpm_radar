"""Evidence-backed event identity shared by report assembly and publication gates.

Similarity retrieves pairs only. Clusters use complete-link agreement so an
ambiguous bridge cannot silently merge two different launches.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

POLICY_VERSION = "events-v1"
PRODUCTION_MODE = "observe"
ENFORCE_FROM = "2026-09-19"
REGISTRY_DAYS = 45
WINDOW_DAYS = 7
HIGH_CONFIDENCE = 0.95
MEDIUM_CONFIDENCE = 0.75
SOURCE_TYPES = {"primary", "news", "review"}
ACTIONS = {"launch", "feature", "deployment", "result", "incident", "research", "policy"}
_LOCALES = {"en", "ru", "de", "fr", "es", "it", "pt", "zh", "ja", "ko", "nl", "pl", "tr"}
_TRACKING = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "amp", "output", "locale", "lang"}
Arbiter = Callable[[dict[str, Any], dict[str, Any], int], object]


class EventDedupError(ValueError):
    """Publication must stop until the event evidence or decision is resolved."""


def normalized(value: object) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


def url_key(value: object) -> str:
    parsed = urlsplit(str(value or ""))
    if not parsed.hostname:
        return ""
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].lower().split("-")[0] in _LOCALES:
        parts.pop(0)
    if parts and parts[-1].lower() == "amp":
        parts.pop()
    path = "/" + "/".join(parts)
    if path.endswith(".amp"):
        path = path[:-4]
    query = sorted(
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in _TRACKING - {"output"}
        and not (key.lower() == "output" and val.lower() == "amp")
    )
    return urlunsplit(("https", host, path, urlencode(query), ""))


def identity(item: Mapping[str, Any]) -> str:
    return str(
        item.get("materialId") or item.get("id") or item.get("material_id") or item.get("url")
    )


def _urls(item: Mapping[str, Any]) -> set[str]:
    return {
        key
        for field in ("url", "canonical_url", "canonicalUrl")
        if (key := url_key(item.get(field)))
    }


def exact_reason(left: Mapping[str, Any], right: Mapping[str, Any]) -> str | None:
    if identity(left) != "None" and identity(left) == identity(right):
        return "material_id"
    if _urls(left) & _urls(right):
        return "normalized_url"
    lt, rt = normalized(left.get("title")), normalized(right.get("title"))
    # A repeated generic heading is not enough to erase a later event.
    ld = str(left.get("published_at") or left.get("publishedAt") or "")[:10]
    rd = str(right.get("published_at") or right.get("publishedAt") or "")[:10]
    if lt and lt == rt and ld and ld == rd:
        return "title_and_date"
    return None


def cheap_deduplicate(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse URL/ID copies before network and LLM work; preserve discovery provenance."""
    groups: list[list[dict[str, Any]]] = []
    ordered = sorted(
        items,
        key=lambda item: (
            item.get("_fulltext_status") == "resolved",
            len(str(item.get("raw_excerpt") or "")),
            identity(item),
        ),
        reverse=True,
    )
    for item in ordered:
        for group in groups:
            if all(
                exact_reason(item, member) in {"material_id", "normalized_url"} for member in group
            ):
                group.append(item)
                break
        else:
            groups.append([item])
    result = []
    for group in groups:
        card = dict(group[0])
        card["exact_members"] = sorted(
            {
                member_id
                for member in group
                for member_id in member.get("exact_members", [identity(member)])
            }
        )
        card["alternative_links"] = sorted(
            {
                str(link)
                for member in group
                for link in [member.get("url"), *member.get("alternative_links", [])]
                if link and link != card.get("url")
            }
        )
        hits = {
            json.dumps(hit, sort_keys=True): hit
            for member in group
            for hit in member.get("source_hits", [])
        }
        if hits:
            card["source_hits"] = list(hits.values())
        result.append(card)
    return result


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 10000 or (not empty and not value.strip()):
        raise EventDedupError(f"EVENT_GATE: invalid {label}")
    return value.strip()


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise EventDedupError(f"EVENT_GATE: invalid {label}")
    return [_text(item, label) for item in value]


def _object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise EventDedupError(f"EVENT_GATE: invalid {label} fields")
    return value


EVENT_KEYS = {
    "subject",
    "action",
    "object",
    "event_date",
    "products",
    "organizations",
    "event_type",
    "evidence",
    "facts",
    "card_title",
    "card_summary",
    "strong_signal",
}


def validate_event(value: object, text: str | None = None) -> dict[str, Any]:
    event = _object(value, EVENT_KEYS, "event")
    for key in ("subject", "object", "event_type", "evidence", "card_title", "card_summary"):
        _text(event[key], key)
    if not isinstance(event["action"], str) or event["action"] not in ACTIONS:
        raise EventDedupError("EVENT_GATE: invalid action")
    if event["event_type"] != event["action"]:
        raise EventDedupError("EVENT_GATE: event_type must use the canonical action category")
    stamp = event["event_date"]
    if stamp is not None:
        try:
            if date.fromisoformat(stamp).isoformat() != stamp:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise EventDedupError("EVENT_GATE: invalid event date") from exc
    for key in ("products", "organizations", "facts"):
        _strings(event[key], key)
    if not isinstance(event["strong_signal"], bool):
        raise EventDedupError("EVENT_GATE: strong_signal must be boolean")
    if text is not None:
        source = " ".join(text.split())
        for quote in [event["evidence"], *event["facts"]]:
            if " ".join(quote.split()) not in source:
                raise EventDedupError("EVENT_GATE: evidence is not a verbatim source fragment")
    return event


def validate_passport(value: object, text: str | None = None) -> dict[str, Any]:
    passport = _object(value, {"source_type", "events", "non_event_reason"}, "passport")
    if not isinstance(passport["source_type"], str) or passport["source_type"] not in SOURCE_TYPES:
        raise EventDedupError("EVENT_GATE: invalid source type")
    if not isinstance(passport["events"], list) or len(passport["events"]) > 30:
        raise EventDedupError("EVENT_GATE: invalid events")
    for event in passport["events"]:
        validate_event(event, text)
    _text(passport["non_event_reason"], "non_event_reason", empty=bool(passport["events"]))
    return passport


def cluster_id(event: Mapping[str, Any]) -> str:
    fields = [normalized(event[key]) for key in ("subject", "action", "object", "event_date")]
    fields.extend(sorted(map(normalized, event["products"])))
    if not event["event_date"]:
        fields.append(normalized(event["evidence"]))
    return "evt_" + hashlib.sha256(json.dumps(fields).encode()).hexdigest()[:24]


def structural_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Only explicit subject/action/object/date agreement can authorize a merge."""
    if any(
        normalized(left[key]) != normalized(right[key])
        for key in ("subject", "action", "object", "event_type")
    ):
        return False
    if not left["event_date"] or not right["event_date"]:
        return False
    delta = abs(
        (date.fromisoformat(left["event_date"]) - date.fromisoformat(right["event_date"])).days
    )
    lp, rp = set(map(normalized, left["products"])), set(map(normalized, right["products"]))
    return delta <= WINDOW_DAYS and (bool(lp & rp) or (not lp and not rp))


def candidate_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["event_date"] and right["event_date"]:
        delta = abs(
            (date.fromisoformat(left["event_date"]) - date.fromisoformat(right["event_date"])).days
        )
        if delta > WINDOW_DAYS:
            return False
    entities_left = {
        normalized(left["subject"]),
        *map(normalized, left["products"]),
        *map(normalized, left["organizations"]),
    }
    entities_right = {
        normalized(right["subject"]),
        *map(normalized, right["products"]),
        *map(normalized, right["organizations"]),
    }
    prose_left = " ".join(str(left[key]) for key in ("card_title", "card_summary", "evidence"))
    prose_right = " ".join(str(right[key]) for key in ("card_title", "card_summary", "evidence"))
    a, b = set(normalized(prose_left).split()), set(normalized(prose_right).split())
    similarity = len(a & b) / max(1, min(len(a), len(b)))
    near_title = (
        len(normalized(left["card_title"]).split()) >= 5
        and SequenceMatcher(
            None, normalized(left["card_title"]), normalized(right["card_title"])
        ).ratio()
        >= 0.96
    )
    return bool(entities_left & entities_right) or similarity >= 0.55 or near_title


def validate_decision(value: object) -> dict[str, Any]:
    result = _object(
        value,
        {
            "same_event",
            "confidence",
            "common_event",
            "unique_facts_left",
            "unique_facts_right",
            "recommended_source",
            "explanation",
        },
        "arbitration",
    )
    if not isinstance(result["same_event"], bool):
        raise EventDedupError("EVENT_GATE: same_event must be boolean")
    confidence = result["confidence"]
    if (
        type(confidence) not in (int, float)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise EventDedupError("EVENT_GATE: invalid confidence")
    _text(result["common_event"], "common_event", empty=not result["same_event"])
    _text(result["explanation"], "explanation")
    _strings(result["unique_facts_left"], "unique_facts_left")
    _strings(result["unique_facts_right"], "unique_facts_right")
    if not isinstance(result["recommended_source"], str) or result["recommended_source"] not in {
        "left",
        "right",
        "neither",
    }:
        raise EventDedupError("EVENT_GATE: invalid recommended_source")
    return result


def compare_events(
    left: dict[str, Any],
    right: dict[str, Any],
    arbiter: Arbiter | None = None,
    *,
    allow_llm_merge: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    if structural_match(left, right) and left["event_date"] == right["event_date"]:
        return "merge", [{"rule": "subject_action_object_product_type_date"}]
    if not candidate_pair(left, right):
        return "separate", []
    # Different extraction labels alone do not prove a later development. A
    # metadata-only launch description can be mislabelled as a product feature.
    # Without distinct known event dates, let the arbiter inspect the evidence.
    if (
        normalized(left["subject"]) == normalized(right["subject"])
        and left["action"] != right["action"]
        and left["event_date"]
        and right["event_date"]
        and left["event_date"] != right["event_date"]
    ):
        return "separate", [{"rule": "different_action"}]
    if arbiter is None:
        return "review", []
    decisions = [validate_decision(arbiter(left, right, 1))]
    first = decisions[0]
    if first["confidence"] < MEDIUM_CONFIDENCE:
        return "separate", decisions
    if first["confidence"] < HIGH_CONFIDENCE:
        decisions.append(validate_decision(arbiter(left, right, 2)))
        if (
            decisions[-1]["confidence"] < HIGH_CONFIDENCE
            or decisions[-1]["same_event"] != first["same_event"]
        ):
            return "review", decisions
    if not decisions[-1]["same_event"]:
        return "separate", decisions
    return ("merge" if allow_llm_merge and structural_match(left, right) else "review"), decisions


def source_rank(
    item: Mapping[str, Any], event: Mapping[str, Any] | None = None
) -> tuple[int, int, int, int, int, str]:
    passport = item["event_passport"]
    full = item.get("_fulltext_status") == "resolved"
    source_type = passport["source_type"]
    events = [event] if event is not None else passport["events"]
    dated = any(value["event_date"] for value in events)
    facts = sum(len(value["facts"]) for value in events)
    # Fulltext is an eligibility override: an unavailable primary never wins.
    return (
        int(full),
        int(source_type == "primary"),
        int(dated),
        facts,
        int(source_type == "news"),
        identity(item),
    )


def deduplicate(
    items: Sequence[dict[str, Any]],
    *,
    history: Sequence[dict[str, Any]] = (),
    arbiter: Arbiter | None = None,
    allow_llm_merge: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return selected cards and an auditable decision record; never mutate input."""
    cards = [dict(item) for item in items]
    audit: dict[str, Any] = {
        "version": POLICY_VERSION,
        "pairs": [],
        "suppressed": [],
        "pending": [],
    }
    for card in cards:
        validate_passport(
            card.get("event_passport"), str(card.get("raw_excerpt") or card.get("summary") or "")
        )
    # Cheap exact checks keep the best source and all alternative links.
    exact_groups: list[list[dict[str, Any]]] = []
    for card in sorted(cards, key=source_rank, reverse=True):
        for exact_group in exact_groups:
            if all(
                exact_reason(card, member) in {"material_id", "normalized_url"}
                or (
                    exact_reason(card, member) == "title_and_date"
                    and len(card["event_passport"]["events"]) == 1
                    and len(member["event_passport"]["events"]) == 1
                    and structural_match(
                        card["event_passport"]["events"][0], member["event_passport"]["events"][0]
                    )
                    and card["event_passport"]["events"][0]["event_date"]
                    == member["event_passport"]["events"][0]["event_date"]
                )
                for member in exact_group
            ):
                exact_group.append(card)
                break
        else:
            exact_groups.append([card])
    cards = []
    for exact_group in exact_groups:
        chosen = exact_group[0]
        chosen["alternative_links"] = sorted(
            {
                str(link)
                for member in exact_group
                for link in [member.get("url"), *member.get("alternative_links", [])]
                if link and link != chosen.get("url")
            }
        )
        chosen["exact_members"] = sorted(
            {
                member_id
                for member in exact_group
                for member_id in member.get("exact_members", [identity(member)])
            }
        )
        cards.append(chosen)
    nodes = [
        (index, event)
        for index, card in enumerate(cards)
        for event in card["event_passport"]["events"]
    ]
    groups: list[list[int]] = []
    matches: dict[tuple[int, int], str] = {}
    for left, right in combinations(range(len(nodes)), 2):
        li, le = nodes[left]
        ri, revent = nodes[right]
        disposition, decisions = compare_events(
            le, revent, arbiter, allow_llm_merge=allow_llm_merge
        )
        matches[left, right] = disposition
        if decisions or disposition == "review":
            audit["pairs"].append(
                {
                    "left": identity(cards[li]),
                    "right": identity(cards[ri]),
                    "left_event": cluster_id(le),
                    "right_event": cluster_id(revent),
                    "disposition": disposition,
                    "decisions": decisions,
                }
            )
        if disposition == "review":
            audit["pending"].append([identity(cards[li]), identity(cards[ri])])
    for node in range(len(nodes)):
        for group in groups:
            if all(matches[min(node, member), max(node, member)] == "merge" for member in group):
                group.append(node)
                break
        else:
            groups.append([node])
    # Complete-link conflicts must be reviewed, not silently split with high scores.
    membership = {node: index for index, group in enumerate(groups) for node in group}
    for (left, right), result in matches.items():
        if result == "merge" and membership[left] != membership[right]:
            audit["pending"].append(
                [identity(cards[nodes[left][0]]), identity(cards[nodes[right][0]])]
            )
    eligible: dict[int, list[dict[str, Any]]] = {}
    for group in groups:
        representative = max(
            group, key=lambda node: source_rank(cards[nodes[node][0]], nodes[node][1])
        )
        owner, event = nodes[representative]
        event_id = min(cluster_id(nodes[node][1]) for node in group)
        parent_id = None
        already_published = False
        for old in history:
            for old_event in old.get("events", []):
                disposition, decisions = compare_events(
                    event, old_event["passport"], arbiter, allow_llm_merge=allow_llm_merge
                )
                if disposition == "review":
                    audit["pending"].append([identity(cards[owner]), old_event["event_cluster_id"]])
                if decisions:
                    audit["pairs"].append(
                        {
                            "left": identity(cards[owner]),
                            "right": old_event["event_cluster_id"],
                            "disposition": disposition,
                            "decisions": decisions,
                        }
                    )
                if disposition == "merge":
                    event_id = old_event["event_cluster_id"]
                    already_published = True
                old_passport = old_event["passport"]
                if (
                    normalized(event["subject"]) == normalized(old_passport["subject"])
                    and set(map(normalized, event["products"]))
                    & set(map(normalized, old_passport["products"]))
                    and event["action"] != old_passport["action"]
                    and event["event_date"]
                    and old_passport["event_date"]
                    and event["event_date"] > old_passport["event_date"]
                ):
                    parent_id = old_event["event_cluster_id"]
        member_cards = {nodes[node][0] for node in group}
        if already_published:
            audit["suppressed"].append(
                {
                    "event_cluster_id": event_id,
                    "reason": "published_in_registry",
                    "materials": sorted(identity(cards[index]) for index in member_cards),
                }
            )
            continue
        alternatives = sorted(
            {
                str(link)
                for index in member_cards
                for link in [cards[index].get("url"), *cards[index].get("alternative_links", [])]
                if link and link != cards[owner].get("url")
            }
        )
        selected = {
            "event_cluster_id": event_id,
            "parent_event_cluster_id": parent_id,
            "passport": event,
            "alternative_links": alternatives,
            "members": sorted(
                {member for index in member_cards for member in cards[index]["exact_members"]}
            ),
            "selection_reason": (
                "Selected by full-text availability, primary source, "
                "confirmed event date, event facts, original journalism and stable identity. "
                f"Winner rank: {source_rank(cards[owner], event)[:5]}."
            ),
            "source_type": cards[owner]["event_passport"]["source_type"],
            "fulltext_verified": cards[owner].get("_fulltext_status") == "resolved",
        }
        eligible.setdefault(owner, []).append(selected)
    selected_cards = []
    for index, card in enumerate(cards):
        source_hash = hashlib.sha256(
            str(card.get("raw_excerpt") or card.get("summary") or "").encode()
        ).hexdigest()
        selected_events = eligible.get(index, [])
        if not selected_events:
            audit["suppressed"].append(
                {"material": identity(card), "reason": "no_unique_event_signal"}
            )
            continue
        is_review = (
            card["event_passport"]["source_type"] == "review"
            or len(card["event_passport"]["events"]) > 1
        )
        if is_review:
            selected_events = [
                event
                for event in selected_events
                if event["passport"]["strong_signal"] and event["passport"]["facts"]
            ]
            if not selected_events:
                audit["suppressed"].append(
                    {"material": identity(card), "reason": "review_without_unique_strong_signal"}
                )
                continue
            # Every published sentence comes from a unique event's editorial projection.
            focus = selected_events[0]["passport"]
            card["original_title"] = card["title"]
            card["title"] = focus["card_title"]
            card["summary"] = "\n\n".join(
                event["passport"]["card_summary"] for event in selected_events
            )
            card["raw_excerpt"] = " ".join(
                event["passport"]["evidence"] for event in selected_events
            )
            for field in ("brief", "llm_summary", "agpm_takeaway"):
                card.pop(field, None)
        card["event_dedup"] = {
            "version": POLICY_VERSION,
            "events": selected_events,
            "source_text_sha256": source_hash,
        }
        card["alternative_links"] = sorted(
            {link for event in selected_events for link in event["alternative_links"]}
        )
        selected_cards.append(card)
    if audit["pending"]:
        # Return review evidence to the caller before its blocking publication gate.
        return selected_cards, audit
    publication_gate(selected_cards, require_events=True)
    return selected_cards, audit


def validate_envelope(value: object) -> dict[str, Any]:
    envelope = _object(value, {"version", "events", "source_text_sha256"}, "event evidence")
    if envelope["version"] != POLICY_VERSION or not re.fullmatch(
        r"[0-9a-f]{64}", str(envelope["source_text_sha256"])
    ):
        raise EventDedupError("EVENT_GATE: invalid event evidence version/hash")
    if not isinstance(envelope["events"], list) or not 1 <= len(envelope["events"]) <= 30:
        raise EventDedupError("EVENT_GATE: card has no selected events")
    for event in envelope["events"]:
        _object(
            event,
            {
                "event_cluster_id",
                "parent_event_cluster_id",
                "passport",
                "alternative_links",
                "members",
                "selection_reason",
                "source_type",
                "fulltext_verified",
            },
            "selected event",
        )
        validate_event(event["passport"])
        for field in ("event_cluster_id", "parent_event_cluster_id"):
            if field == "parent_event_cluster_id" and event[field] is None:
                continue
            if not re.fullmatch(r"evt_[0-9a-f]{24}", str(event[field])):
                raise EventDedupError("EVENT_GATE: invalid cluster id")
        if event["parent_event_cluster_id"] == event["event_cluster_id"]:
            raise EventDedupError("EVENT_GATE: self-parent event")
        if not _strings(event["members"], "members"):
            raise EventDedupError("EVENT_GATE: event cluster has no members")
        _strings(event["alternative_links"], "alternative_links")
        _text(event["selection_reason"], "selection_reason")
        if (
            not isinstance(event["source_type"], str)
            or event["source_type"] not in SOURCE_TYPES
            or not isinstance(event["fulltext_verified"], bool)
        ):
            raise EventDedupError("EVENT_GATE: invalid source evidence")
    return envelope


def publication_gate(
    cards: Sequence[Mapping[str, Any]],
    *,
    require_events: bool = False,
    history: Sequence[dict[str, Any]] = (),
) -> None:
    events: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, card in enumerate(cards):
        value = card.get("eventDedup", card.get("event_dedup"))
        if value is None:
            if require_events:
                raise EventDedupError(f"EVENT_GATE: missing passport: {identity(card)}")
            continue
        envelope = validate_envelope(value)
        for event in envelope["events"]:
            if event["event_cluster_id"] in seen:
                raise EventDedupError("EVENT_GATE: duplicate event cluster")
            seen.add(event["event_cluster_id"])
            events.append((index, event))
            for old in history:
                for prior in old["events"]:
                    if prior["event_cluster_id"] == event["event_cluster_id"] or (
                        structural_match(event["passport"], prior["passport"])
                        and event["passport"]["event_date"] == prior["passport"]["event_date"]
                    ):
                        raise EventDedupError("EVENT_GATE: event was already published")
    for left, right in combinations(cards, 2):
        if reason := exact_reason(left, right):
            raise EventDedupError(f"EVENT_GATE: duplicate {reason}")
    for (li, left), (ri, right) in combinations(events, 2):
        if (
            li != ri
            and structural_match(left["passport"], right["passport"])
            and left["passport"]["event_date"] == right["passport"]["event_date"]
        ):
            raise EventDedupError("EVENT_GATE: overlapping event cards (including fallback/review)")


def production_gate(
    cards: Sequence[Mapping[str, Any]],
    *,
    require_events: bool = False,
    history: Sequence[dict[str, Any]] = (),
) -> None:
    """Keep identity invariants; semantic decisions are observations in production."""
    if PRODUCTION_MODE != "observe":
        publication_gate(cards, require_events=require_events, history=history)
        return
    for left, right in combinations(cards, 2):
        reason = exact_reason(left, right)
        if reason in {"material_id", "normalized_url"}:
            raise EventDedupError(f"EVENT_GATE: duplicate {reason}")
