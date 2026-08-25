"""The read-only service behind the agent mode (stage 3).

Everything the third position of the switcher on radar.agpm.space needs, and
nothing else. It binds to loopback, Caddy puts it on `/kb/*`, and it connects as
`radar_kb_public` - a role that has USAGE on the `agent` schema and no privilege
anywhere in `kx` (migration 024). That is the difference between a service that
does not ask for other people's full text and one that could not return it if it
were wrong.

**Four levels of disclosure** (UC-02), because a reader who asks a question and a
reader checking whether to believe the answer want different amounts:

1. the answer, marked as machine-written, with the statements under it;
2. each statement with its labels - what kind of material, whose claim it
   originally was, what status it holds, what date is shown and which date that is;
3. the exact quotation and the character range it occupies in the source;
4. the source itself: title, link, and the trail back to the issue it entered by.

Every level is a field of the same response rather than four endpoints, so a
client cannot show level one and quietly fail to fetch level three.

**What the reader is told about the answer.** Decision 6: an agent's answer is
free text, marked "агентный ответ, не редакция базы", with the quotations under
it. The wording was "машинный" until ADR-0013 - the same promise, in the word the
rest of the site uses for the thing that made the answer.

Decision 9: the chat is a chat - questions and answers are kept for analysis,
there is no permanent address for an answer and no public retraction procedure,
because nothing here is published under the base's name.

A question is data, not an instruction (ADR-0005 §15), and so is every quotation
inside an evidence package.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg

from radar_kx.agent_chat import (
    PROMPTS_ON_WELCOME,
    TOOL_CONCEPT,
    TOOL_CONTRA,
    TOOL_GAPS,
    TOOL_WATCH,
    select_tool,
    tool_card_limit,
    valid_session,
    welcome_prompts,
)
from radar_kx.config import Settings
from radar_kx.database import ACCESS_KEY_ENTROPY_BYTES, ACCESS_KEY_PREFIX, Database
from radar_kx.orchestrator import RESEARCH_ANSWER, ModelGateway, OrchestratorError
from radar_kx.research import (
    PACKAGE_SIZE,
    build_answer_prompt,
    build_package,
    refuse,
    render,
    verify,
)
from radar_kx.research import parse_answer as parse_research_answer

MAX_BODY_BYTES = 8 * 1024

#: How many model-backed answers one client may ask for in a window, and how many
#: the service may produce in a day for everyone together. `/kb/ask` reaches a
#: paid model with no login in front of it, so these two numbers are the whole of
#: what stands between a `for` loop and the bill.
ASKS_PER_CLIENT = 10
ASK_WINDOW_SECONDS = 300.0
#: No ceiling on the day. The owner's call: the point of the base is that people
#: use it, and a limit that turns the agent off at four in the afternoon is a
#: worse failure than the bill it was guarding. What stays is the per-client
#: window, which is not a budget - it is what stands between a `for` loop and the
#: endpoint, and it lets a person hold a conversation while refusing a script.
DAILY_ASK_BUDGET = 0

#: Longest question accepted. A question is one question; a page of text pasted
#: into the box is a way to spend the model budget, not a way to ask.
MAX_QUESTION_CHARS = 500

#: How many hits a search returns at most. The reader is looking for evidence,
#: not browsing a corpus.
MAX_HITS = 50

#: The owner's standing model (2026-08-25): the subscription is membership,
#: not monetisation. The project is free and non-commercial; a key says
#: «this reader is of the community», and membership buys capabilities and a
#: hand in the canon - not the corpus. Three consequences, written down so
#: the list stops oscillating:
#:
#: 1. Reading that the dialogue itself does is free at the source: a
#:    conversation expands what it shows - one known node's neighbourhood
#:    (`/graph`), one statement with its links (`/statement`) - and there is
#:    no sense fencing one of the two paths the chat walks by. Measured
#:    2026-08-24: even with `/graph` gated, `/statement/<id>` kept handing
#:    out statement texts with neighbours - a fence with the gate open is
#:    noise, not a wall.
#: 2. What the key opens is apparatus, not data: the shelves (search,
#:    observatory, contradictions, topics, wiki, gaps), a wider conversation
#:    window, and later the feeds. A member's difference is what they can
#:    *do*, and what they owe: participation in the canon's development.
#: 3. Nothing here is a payment boundary. If a script wants the texts, the
#:    texts are not what the community is for.
GATED_PATHS: Final = frozenset(
    {
        "/search",
        "/topics",
        "/observatory",
        "/entities",
        "/contradictions",
        "/gaps",
        "/pages",
    }
)
GATED_PREFIXES: Final = ("/topics/", "/pages/")


#: A subscriber's conversation window. Higher than the free one by the owner's
#: call - and still a window, not a budget: it stands between a shared key and
#: the bill, nothing more.
#: Сколько живёт запомненный счёт объектов. Цепочка проходит раз в сутки, так
#: что десять минут не дают устареть ничему, кроме первых минут после прохода.
COUNTS_TTL_SECONDS: Final = 600.0

SUBSCRIBER_ASKS_PER_CLIENT = 30

#: And a ceiling on the key itself, per day. The window above counts clients, so
#: it stops one person and not one key: a key that has spread to a hundred
#: addresses buys a hundred windows, and there is no daily budget behind it. This
#: is what makes a leaked key finite rather than free.
SUBSCRIBER_ASKS_PER_KEY_PER_DAY = 500

#: Key shape, checked before any digest is computed: shaping first keeps the
#: database away from arbitrary probe strings.
#:
#: The length is derived from the generator rather than written beside it. Set by
#: hand it was 42 against a real key of 46, and every key the owner issued was
#: refused before the database was ever asked - while the tests, which built
#: their keys out of this same constant, stayed green through it.
KEY_PREFIX = ACCESS_KEY_PREFIX
KEY_LENGTH = len(KEY_PREFIX) + len(secrets.token_urlsafe(ACCESS_KEY_ENTROPY_BYTES))


class AccessGuard:
    """Is this request holding a live subscription key?

    The answer is cached in-process for a minute, on the same reasoning as
    `AskBudget`: one process on one host, and a cache whose whole job is to be
    approximately right. A revocation lands within the minute; an issuance is
    seen immediately, because a key nobody has probed with has no cache entry.

    The key itself never travels past the digest. An invalid result is cached
    too - refusing twice costs nothing, and the cache must not turn into an
    oracle that distinguishes "never seen" from "seen and refused".
    """

    def __init__(self, database: Database, *, ttl_seconds: float = 60.0, limit: int = 4096) -> None:
        self._database = database
        self._ttl = ttl_seconds
        self._limit = limit
        self._cache: dict[str, tuple[bool, str | None, Any, float]] = {}
        self._lock = threading.Lock()

    def check(self, authorization: str | None) -> dict[str, Any]:
        header = (authorization or "").strip()
        if not header.startswith("Bearer "):
            return {"valid": False, "plan": None, "expiresAt": None, "digest": None}
        key = header[len("Bearer ") :].strip()
        if not key.startswith(KEY_PREFIX) or len(key) != KEY_LENGTH:
            return {"valid": False, "plan": None, "expiresAt": None, "digest": None}
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(digest)
            if cached is not None and now - cached[3] < self._ttl:
                return {
                    "valid": cached[0],
                    "plan": cached[1],
                    "expiresAt": cached[2],
                    "digest": digest,
                }
        found = self._database.access_key(digest)
        answer: dict[str, Any] = {
            "valid": found is not None,
            "plan": found and found.get("plan"),
            "expiresAt": found and found.get("expires_at"),
        }
        with self._lock:
            # Keyed by a digest of whatever the caller sent, so the cache is a
            # place an unauthenticated stranger can put things. Bounded, and the
            # oldest go first: a probe loop must not be able to grow it forever.
            if len(self._cache) >= self._limit:
                for stale in sorted(self._cache, key=lambda seen: self._cache[seen][3])[
                    : max(1, self._limit // 4)
                ]:
                    self._cache.pop(stale, None)
            self._cache[digest] = (answer["valid"], answer["plan"], answer["expiresAt"], now)
        return {**answer, "digest": digest}


#: The two sentences that travel with every generated answer. The owner's wording
#: (decision 6, reworded by ADR-0013), kept here as a constant rather than in a
#: template, so no client can render an answer without them.
#:
#: The name of the constant and the `machineNotice` field it fills are unchanged
#: on purpose: the field is a wire contract the front end reads, and renaming it
#: would break every client to say the same thing. What the reader sees is the
#: string, and the string is what the decision is about.
MACHINE_NOTICE = "Агентный ответ, не редакция базы."
SIGNATURE = "AgPM Radar, агентная сборка"

#: What a question can be aimed at. Two of these are admissions the reading pass
#: assigns and decision 3 keeps apart - knowledge, and the market chronicle; the
#: third is the reader asking both shelves at once (ADR-0012).
#:
#: `all` is a *named* value, not an absence, and the difference is the whole
#: rule: a value nobody named still narrows to knowledge, so a typo, an old
#: client or a truncated field cannot widen the search by accident. Only a
#: reader who asked for both gets both.
#:
#: And `all` reaches no further than the two shelves. `agent.statement` is built
#: `WHERE reading.admission <> 'rejected'` (kx/sql/024_agent_surface.sql), so
#: what the reading pass threw out and what it never read are outside every
#: search here, whatever this parameter says.
ADMISSION_SCOPES: Final = ("knowledge", "observatory", "all")

#: The licence line the base carries (decision 10). Attribution for the
#: reworking and the structure; a quotation stays its rightholder's and always
#: travels with its link.
LICENCE = (
    "Переработка и структура — свободно со ссылкой на AgPM Radar. "
    "Цитаты остаются за правообладателями и приводятся со ссылкой на источник."
)


class AskBudget:
    """What a public, unauthenticated, model-backed endpoint is allowed to spend.

    `/kb/ask` reaches a paid model. It has no login by the owner's decision, so
    the only thing between a `for` loop and the model bill is this. Two limits,
    because they fail differently:

    * **per client, per window** - one caller cannot monopolise the endpoint;
    * **per day, for everyone** - a thousand callers each under the per-client
      limit still add up, and a budget that only bounds individuals bounds
      nothing.

    Neither is a security boundary. A client is a proxy header and can be
    spoofed; the daily cap is what actually holds when it is. Cached answers are
    free and never counted: the same question asked twice costs one call.

    In-process on purpose. The service is one process on one host, and a shared
    counter would be a second thing to run and to keep correct for a limit whose
    whole job is to be approximately right.
    """

    def __init__(
        self,
        *,
        per_client: int = ASKS_PER_CLIENT,
        window: float = ASK_WINDOW_SECONDS,
        per_day: int = DAILY_ASK_BUDGET,
    ) -> None:
        self._per_client = per_client
        self._window = window
        self._per_day = per_day
        self._asked: dict[str, list[float]] = {}
        #: Per key, per day. The window above counts addresses; this counts the
        #: key, which is the only thing a leaked key cannot change.
        self._key_day: dict[str, int] = {}
        self._day: tuple[int, int] = (0, 0)
        self._lock = threading.Lock()

    def _today(self, moment: float) -> int:
        return int(moment // 86400)

    def refused(
        self,
        client: str,
        *,
        now: float | None = None,
        allowance: int | None = None,
        key: str | None = None,
        key_ceiling: int | None = None,
    ) -> str | None:
        """Why this call may not be made, or `None` if it may.

        Counts the call when it allows it: a check that does not consume is a
        check every concurrent request passes. `allowance` widens the window for
        this one caller - a subscriber - without giving anyone a second bucket:
        free calls and paid calls land in the same window, so a client cannot
        double its reach by alternating keys.
        """
        moment = time.time() if now is None else now
        today = self._today(moment)
        with self._lock:
            day, spent = self._day
            if day != today:
                day, spent = today, 0
                self._asked.clear()
                self._key_day.clear()
            # `0` means no ceiling on the day. The per-client window below is
            # what remains, and it is an abuse limit rather than a budget.
            if self._per_day and spent >= self._per_day:
                self._day = (day, spent)
                return "today"
            if key and key_ceiling and self._key_day.get(key, 0) >= key_ceiling:
                self._day = (day, spent)
                return "key"
            recent = [at for at in self._asked.get(client, []) if moment - at < self._window]
            ceiling = allowance if allowance is not None else self._per_client
            if len(recent) >= ceiling:
                self._asked[client] = recent
                self._day = (day, spent)
                return "client"
            recent.append(moment)
            self._asked[client] = recent
            if key:
                self._key_day[key] = self._key_day.get(key, 0) + 1
            self._day = (day, spent + 1)
        return None


#: Шаги суточной цепочки, все четыре. База свежа настолько, насколько свеж её
#: самый отставший шаг, поэтому список полный: шаг, который ни разу не прошёл,
#: обязан обнулить ответ, а не выпасть из подсчёта минимума.
CHAIN_STEPS: Final = ("perimeter", "ingest", "knowledge", "embedding")


def _synced_at(chain: list[dict[str, Any]]) -> str | None:
    """Когда база синхронизирована - по самому отставшему шагу цепочки.

    Пусто, пока хотя бы один шаг ни разу не прошёл удачно. Это не осторожность:
    ingest срабатывает каждые полчаса, и минимум по «тем шагам, что прошли»
    объявил бы базу свежей на полчаса, пока суточное звено знания молчит вторые
    сутки. Ровно так однажды пасс писал success при calls: 0.
    """
    when = {}
    for row in chain:
        step = str(row.get("step") or "")
        if row.get("succeeded_at"):
            when[step] = row["succeeded_at"]
    if any(step not in when for step in CHAIN_STEPS):
        return None
    return min(str(when[step]) for step in CHAIN_STEPS)


class AgentService:
    """What the agent mode may ask for. Reads; the only writes are its own log."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        #: Loaded once on the first question and kept. Loading e5 takes seconds;
        #: doing it per question would make the semantic arm the slow part.
        self._model: Any = None
        self._model_lock = threading.Lock()
        self.budget = AskBudget()
        #: Key validation has its own small window, so that probing keys cannot
        #: spend anybody's question allowance - and cannot run unthrottled.
        self.validate_budget = AskBudget(per_client=10, window=300.0, per_day=0)
        self.guard = AccessGuard(database)
        #: Числа объектов базы. Считаются по всей поверхности, меняются раз в
        #: сутки - значит, живут в памяти процесса, а не в каждом запросе.
        self._counts: dict[str, Any] | None = None
        self._counts_at = 0.0

    def access(self, authorization: str | None) -> dict[str, Any]:
        """The one door every handler asks through, free paths included."""
        return self.guard.check(authorization)

    # -- level 4: the backbone and the shelves ---------------------------------

    def counts(self) -> dict[str, Any]:
        """How much the base holds, and when it was last synchronised. ADR-0011.

        The count runs over the whole surface and takes about half a second,
        while the base changes once a day, when the chain passes. So it is kept
        for COUNTS_TTL_SECONDS: the first reader pays for it and the rest do
        not, and the number is never staler than one pass of the chain.
        """
        now = time.monotonic()
        if self._counts is not None and now - self._counts_at < COUNTS_TTL_SECONDS:
            return self._counts
        counted = dict(self.database.agent_counts())
        chain = [dict(row) for row in self.database.agent_sync()]
        self._counts = {**counted, "chain": chain, "syncedAt": _synced_at(chain)}
        self._counts_at = now
        return self._counts

    def topics(self) -> dict[str, Any]:
        return {"topics": self.database.agent_topics(), "signature": SIGNATURE}

    def concept(self, topic_key: str) -> dict[str, Any]:
        """One card of the backbone: what the base holds under this subject."""
        card = self.database.agent_concept(topic_key)
        if card is None:
            return {"error": "нет такой темы", "topicKey": topic_key}
        return {**card, "signature": SIGNATURE, "licence": LICENCE}

    def observatory(
        self, *, since: str | None, until: str | None, kind: str | None, fresh: bool = False
    ) -> dict[str, Any]:
        """A cut by class of event over a period - decision 4, not a feed."""
        return {
            "observatory": self.database.agent_observatory(
                since=since, until=until, kind=kind, fresh=fresh
            ),
            "signature": SIGNATURE,
            "licence": LICENCE,
        }

    def graph(
        self, *, claim: str | None, topic: str | None, entity: str | None, limit: int
    ) -> dict[str, Any]:
        """UC-05: what one thing is connected to, as a picture rather than a list."""
        found = self.database.agent_graph(
            claim_id=claim, topic_key=topic, entity_id=entity, limit=limit
        )
        return {**found, "signature": SIGNATURE, "licence": LICENCE}

    def entities(self, *, kind: str | None, limit: int) -> dict[str, Any]:
        """Who and what the base talks about, most named first."""
        return {
            "entities": self.database.agent_entities(kind=kind, limit=limit),
            "signature": SIGNATURE,
            "licence": LICENCE,
        }

    def contradictions(self, limit: int) -> dict[str, Any]:
        """Where the base disagrees with itself, both sides at once (UC-11)."""
        total, pairs = self.database.agent_contradictions(limit=limit)
        return {"total": total, "pairs": pairs, "signature": SIGNATURE, "licence": LICENCE}

    def gaps(self, limit: int) -> dict[str, Any]:
        return {"gaps": self.database.agent_gaps(limit=limit)}

    def pages(self) -> dict[str, Any]:
        return {"pages": self.database.agent_pages()}

    def page(self, path: str) -> dict[str, Any]:
        page = self.database.agent_page(path)
        if page is None:
            return {"error": "нет такой страницы", "path": path}
        return {**page, "signature": "автор методики — владелец базы"}

    # -- levels 1-3: search and the agent ---------------------------------------

    def search(
        self, question: str, *, filters: dict[str, str | None], limit: int
    ) -> dict[str, Any]:
        """Evidential search: what was found, and why each thing was found."""
        if not question.strip():
            return {"error": "пустой запрос"}
        # With no vector the meaning arm excludes itself - `WHERE
        # question_vector IS NOT NULL` - and the whole of UC-01's "three arms"
        # collapses to two lexical ones. `ask` passed one from the start and
        # `search` never did, so the same phrase answered twice over: through
        # `ask`, "слова, смысл" and statements about delegated autonomy; through
        # `search`, "слова" and whatever shared a word stem.
        hits = self.database.agent_search(
            question[:MAX_QUESTION_CHARS],
            filters=filters,
            limit=min(limit, MAX_HITS),
            question_vector=self._vector(question[:MAX_QUESTION_CHARS]),
        )
        return {"query": question[:MAX_QUESTION_CHARS], "hits": hits, "licence": LICENCE}

    def ask(
        self,
        question: str,
        *,
        client: str = "unknown",
        admission: str = "knowledge",
        asks_per_client: int | None = None,
        key_digest: str | None = None,
    ) -> dict[str, Any]:
        """The agent's own answer: the last event of the flow below, nothing more.

        Kept as its own method because `/ask` is a frozen public contract; the
        conversation layer builds on the same flow without touching it.
        """
        result: dict[str, Any] = {"error": "пустой вопрос"}
        for event, payload in self._answer_flow(
            question,
            client=client,
            admission=admission,
            asks_per_client=asks_per_client,
            key_digest=key_digest,
        ):
            if event == "result":
                result = payload
        return result

    def _answer_flow(
        self,
        question: str,
        *,
        client: str,
        admission: str,
        asks_per_client: int | None = None,
        key_digest: str | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """The verified pipeline as a stream of stages with the answer last.

        The order is the owner's, not the model's: the base retrieves, the model
        drafts clauses and says which numbered quotation each rests on, and code
        checks that claim against the spans before anything is returned. A draft
        that does not survive the check becomes a refusal (ADR-0004 §9) rather
        than a hedged sentence, and what the base does hold nearby is returned
        beside it as its own field.

        Each stage is yielded as `("stage", {...})` the moment it completes, so
        the conversation endpoint can show the conveyor live; `("result", ...)`
        closes the flow. `ask` drains it; the SSE route streams it.
        """
        question = question.strip()[:MAX_QUESTION_CHARS]
        if not question:
            yield "result", {"error": "пустой вопрос"}
            return
        # Decision 3 keeps knowledge and the market chronicle apart, and the
        # reader chooses what the question is aimed at: one shelf, or - since
        # ADR-0012 - both. Anything else is knowledge: an unknown value must not
        # quietly widen the search, and `all` widens only because it was named.
        if admission not in ADMISSION_SCOPES:
            admission = "knowledge"

        # ADR-0006 §10 keys the cache by scope, because a cache without it moves
        # content between access levels and does so silently. The shelf the
        # reader picked is one of those levels: "что нового" aimed at knowledge
        # and aimed at the chronicle are two questions with two right answers,
        # and the first one asked must not be served as the second.
        scope = f"public:{admission}"

        cached = self.database.cached_answer(question, scope=scope)
        if cached is not None:
            yield "stage", {"step": "search", "done": True, "hits": 0, "cache": True}
            yield (
                "result",
                self._as_answer(
                    question,
                    answer_text=cached.get("answer_text"),
                    refusal_reason=cached.get("refusal_reason"),
                    package=cached.get("evidence_package") or [],
                    verification=cached.get("verification"),
                    from_cache=True,
                ),
            )
            return

        # Only a question the cache cannot answer costs anything, so the budget
        # is charged here rather than at the door: asking the same thing twice is
        # free and should stay free.
        # A subscriber's window is wider (the owner's call) - and it is still
        # the same window, per client, not per key: a shared key shares it.
        refused = self.budget.refused(
            client,
            allowance=asks_per_client,
            key=key_digest,
            key_ceiling=SUBSCRIBER_ASKS_PER_KEY_PER_DAY if key_digest else None,
        )
        if refused is not None:
            yield "result", self._as_answer(question, refusal_reason=f"rate_limited_{refused}")
            return

        hits = self.database.agent_search(
            question,
            # `all` is the absence of the filter, which is what widens it; the
            # view underneath has already dropped what was never admitted.
            filters={"admission": None if admission == "all" else admission},
            limit=PACKAGE_SIZE,
            question_vector=self._vector(question),
        )
        package = build_package(hits, size=PACKAGE_SIZE)
        yield "stage", {"step": "search", "done": True, "hits": len(package), "cache": False}
        if not package:
            refusal = refuse("no_evidence", "в базе нет подходящих подтверждений")
            self.database.record_answer(
                question=question,
                scope=scope,
                mode="strict",
                package=(),
                refusal=refusal,
                answered_by="radar-kb-agent",
            )
            yield "result", self._as_answer(question, refusal_reason=refusal.reason, package=[])
            return

        gateway = ModelGateway(self.database, self.settings)
        result = gateway.run(RESEARCH_ANSWER, build_answer_prompt(question, package))
        clauses = parse_research_answer(result.content)
        yield "stage", {"step": "draft", "done": True}
        checked = verify(clauses, package, mode="strict")
        yield "stage", {"step": "verify", "done": True, "passes": bool(checked.passes)}
        if not clauses or not checked.passes:
            refusal = refuse(
                "no_evidence",
                "черновик не прошёл проверку по цитатам" if clauses else "евидентной базы нет",
                package,
            )
            self.database.record_answer(
                question=question,
                scope=scope,
                mode="strict",
                package=package,
                refusal=refusal,
                verification=checked,
                model=RESEARCH_ANSWER.model,
                answered_by="radar-kb-agent",
            )
            yield (
                "result",
                self._as_answer(
                    question,
                    refusal_reason=refusal.reason,
                    package=[element.as_json() for element in package],
                    verification=checked.as_json(),
                ),
            )
            return

        answer_text = render(clauses)
        self.database.record_answer(
            question=question,
            scope=scope,
            mode="strict",
            package=package,
            answer_text=answer_text,
            verification=checked,
            model=RESEARCH_ANSWER.model,
            answered_by="radar-kb-agent",
        )
        # `render` joins clause texts and drops their evidence numbers, which is
        # right for a flat answer and wrong for a conversation, where a clause
        # must remain clickable down to its quotation. The clauses travel
        # structured, beside the rendered text, and nothing about `/ask` changes.
        yield (
            "result",
            {
                **self._as_answer(
                    question,
                    answer_text=answer_text,
                    package=[element.as_json() for element in package],
                    verification=checked.as_json(),
                ),
                "clauses": [
                    {"text": clause.text, "evidence": list(clause.evidence)} for clause in clauses
                ],
            },
        )

    # -- the conversation layer --------------------------------------------------

    def prompts(
        self, *, count: int = PROMPTS_ON_WELCOME, seed: int | None = None
    ) -> dict[str, Any]:
        """The welcome screen's examples, sampled from a pool that follows the base.

        Free by construction: the pool is assembled from the topic skeleton the
        service already serves, and no model is involved.
        """
        sampled = welcome_prompts(self.database.agent_topics(), count=count, seed=seed)
        # Числа едут вместе с примерами, потому что диалог всё равно ходит сюда
        # при открытии: отдельный запрос ради счётчиков был бы вторым кругом.
        return {**sampled, "counts": self.counts()}

    def chat(
        self,
        question: str,
        *,
        client: str = "unknown",
        admission: str = "knowledge",
        session: str = "",
        asks_per_client: int | None = None,
        key_digest: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """One turn of the conversation: live stages, then the answer with its cards.

        The verified pipeline is the spine of every turn and runs unchanged. What
        the conversation adds is around it: the stages it passed through, a tool
        card chosen deterministically from the question (a named subject gets its
        concept card, a contradiction question gets both sides), and the session
        label travelling back to the client untouched - stored nowhere, per the
        owner's decision that a chat is analysis material, not publication.
        """
        if not valid_session(session):
            return [], {"error": "недопустимый идентификатор сессии"}
        topics = self.database.agent_topics()
        choice = select_tool(question, topics)
        stages: list[dict[str, Any]] = []
        result: dict[str, Any] = {"error": "пустой вопрос"}
        for event, payload in self._answer_flow(
            question,
            client=client,
            admission=admission,
            asks_per_client=asks_per_client,
            key_digest=key_digest,
        ):
            if event == "stage":
                stages.append(payload)
            elif event == "result":
                result = payload
        if "error" in result:
            return stages, result
        cards = self._tool_cards(choice, limit=tool_card_limit(question))
        return stages, {
            **result,
            "session": session,
            "tool": choice.tool,
            "toolBecause": choice.because,
            "toolCards": cards,
            "stages": stages,
        }

    def chat_events(
        self,
        question: str,
        *,
        client: str = "unknown",
        admission: str = "knowledge",
        session: str = "",
        asks_per_client: int | None = None,
        key_digest: str | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """The same turn as `chat`, yielding stages as they complete (SSE)."""
        if not valid_session(session):
            yield "error", {"error": "недопустимый идентификатор сессии"}
            return
        topics = self.database.agent_topics()
        choice = select_tool(question, topics)
        stages: list[dict[str, Any]] = []
        for event, payload in self._answer_flow(
            question,
            client=client,
            admission=admission,
            asks_per_client=asks_per_client,
            key_digest=key_digest,
        ):
            if event == "stage":
                stages.append(payload)
                yield event, payload
            elif event == "result":
                if "error" in payload:
                    yield "result", payload
                    return
                cards = self._tool_cards(choice, limit=tool_card_limit(question))
                yield (
                    "result",
                    {
                        **payload,
                        "session": session,
                        "tool": choice.tool,
                        "toolBecause": choice.because,
                        "toolCards": cards,
                        "stages": stages,
                    },
                )

    def _tool_cards(self, choice: Any, *, limit: int) -> list[dict[str, Any]]:
        """A card is data from the base, not a model claim, so it carries no
        verification and needs none: it is the evidence, shown directly."""
        if choice.tool == TOOL_CONCEPT and choice.topic_key:
            card = self.concept(choice.topic_key)
            return [{"type": "concept", "data": card}] if "error" not in card else []
        if choice.tool == TOOL_CONTRA:
            return [{"type": "contradictions", "data": self.contradictions(limit)}]
        if choice.tool == TOOL_GAPS:
            return [{"type": "gaps", "data": self.gaps(limit)}]
        if choice.tool == TOOL_WATCH:
            return [
                {
                    "type": "observatory",
                    "data": self.observatory(since=None, until=None, kind=None),
                }
            ]
        return []

    def _vector(self, question: str) -> str | None:
        """The question as a vector, when this runtime can make one.

        torch lives in the embedder runtime and not in the worker's. The service
        runs under whichever it was started with, and the semantic arm switches
        itself off rather than failing when it is the wrong one.
        """
        try:
            from radar_kx.embeddings import encode, load_model, to_pgvector
        except ImportError:  # pragma: no cover - depends on the runtime
            return None
        try:
            # The whole call, not just the load. One model object served every
            # thread of a ThreadingHTTPServer, and `encode` runs a torch forward
            # pass on shared state. Encoding one short question is milliseconds,
            # so serialising it costs nothing worth measuring.
            with self._model_lock:
                if self._model is None:
                    self._model = load_model()
                return to_pgvector(encode(self._model, [question], is_query=True)[0])
        except Exception:  # pragma: no cover - a missing model is not a failed answer
            # Still not a failed answer: the arm switches off and the search stays
            # word-only, by design. But it says so. Swallowing this silently meant
            # a broken embedder degraded every answer in the same way a runtime
            # without torch does, and nothing anywhere could tell the two apart.
            sys.stderr.write(f"semantic arm off:\n{traceback.format_exc()}")
            return None

    @staticmethod
    def _as_answer(
        question: str,
        *,
        answer_text: str | None = None,
        refusal_reason: str | None = None,
        package: Any = (),
        verification: Any = None,
        from_cache: bool = False,
    ) -> dict[str, Any]:
        """The four levels, in one response, with the notice that cannot be dropped."""
        return {
            "question": question,
            "answer": answer_text,
            "refusalReason": refusal_reason,
            "machineNotice": MACHINE_NOTICE,
            "signature": SIGNATURE,
            "licence": LICENCE,
            "evidence": list(package),
            "verification": verification,
            "fromCache": from_cache,
        }

    def statement(self, claim_id: str) -> dict[str, Any]:
        """One statement, all four levels of it, plus what it is linked to."""
        found = self.database.agent_statement(claim_id)
        if found is None:
            return {"error": "нет такого утверждения", "claimId": claim_id}
        # Путь до выпуска - последнее звено цепочки доверия, и он такой же
        # читаемый факт, как цитата: какой выпуск радара выбрал этот материал.
        return {**found, "trail": self.database.agent_trail(claim_id), "licence": LICENCE}


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items() if values}


def make_handler(service: AgentService) -> type[BaseHTTPRequestHandler]:
    """The routing table, small enough to read in one screen."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "radar-kb"
        sys_version = ""

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            """Every read answers, including the reads that cannot be served.

            A value that is not a uuid, where a uuid is compared, raised out of
            the routing below - and a handler that raises is answered by
            `BaseHTTPRequestHandler` with a log line and a closed socket. The
            caller got no status line at all, and Caddy turned that into a 502:
            `/graph?claim=x` and `/statement/x` both did it. The POST side has
            said for a while that a public endpoint owes an answer even when the
            answer is "no"; this is the same debt, on the side that only reads.

            The two cases are told apart because they belong to different
            people: a malformed identifier is the caller's, and says so; anything
            else is ours, and must not be dressed up as the caller's mistake.
            """
            try:
                self._route_get()
            except psycopg.DataError:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "идентификатор не распознан", "path": urlsplit(self.path).path},
                )
            except Exception:
                # `log_message` is deliberately silent, so the traceback goes
                # straight to stderr, which is what systemd journals.
                sys.stderr.write(f"read failed:\n{traceback.format_exc()}")
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "служба сейчас недоступна"})

        def _route_get(self) -> None:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            parameters = _query(self.path)
            if path in ("/", "/health"):
                self._json(HTTPStatus.OK, {"service": "radar-kb", "status": "ok"})
                return
            # Числа объектов базы - до стены подписки, и стоят они здесь, выше
            # проверки ключа, а не просто вне `GATED_PATHS`. Список гейтирования
            # за сутки уже качнулся туда и обратно; решение владельца «числа
            # доступны всем» (ADR-0011) не должно зависеть от его следующей
            # правки. Число - не содержание: «11 759 утверждений» не выдаёт ни
            # одного из них, зато говорит читателю, что стоит за замком.
            if path == "/counts":
                self._json(HTTPStatus.OK, service.counts())
                return
            # The subscription wall, before any routing: browsing endpoints answer
            # only with a live key, and the refusal is one machine-readable shape.
            # The conversation paths (/ask, /chat, /prompts) are not here, by the
            # owner's decision - the dialogue stays free.
            if path in GATED_PATHS or path.startswith(GATED_PREFIXES):
                access = service.access(self.headers.get("Authorization"))
                if not access["valid"]:
                    self._json(
                        HTTPStatus.FORBIDDEN,
                        {"error": "subscription_required", "path": path},
                    )
                    return
            if path == "/topics":
                self._json(HTTPStatus.OK, service.topics())
                return
            if path.startswith("/topics/"):
                self._json(HTTPStatus.OK, service.concept(unquote(path[len("/topics/") :])))
                return
            if path == "/observatory":
                self._json(
                    HTTPStatus.OK,
                    service.observatory(
                        since=parameters.get("since"),
                        until=parameters.get("until"),
                        kind=parameters.get("kind"),
                        fresh=parameters.get("fresh") == "1",
                    ),
                )
                return
            if path == "/graph":
                self._json(
                    HTTPStatus.OK,
                    service.graph(
                        claim=parameters.get("claim"),
                        topic=parameters.get("topic"),
                        entity=parameters.get("entity"),
                        limit=_int(parameters.get("limit"), 40),
                    ),
                )
                return
            if path == "/entities":
                self._json(
                    HTTPStatus.OK,
                    service.entities(
                        kind=parameters.get("kind"),
                        limit=_int(parameters.get("limit"), 60),
                    ),
                )
                return
            if path == "/contradictions":
                self._json(
                    HTTPStatus.OK,
                    service.contradictions(_int(parameters.get("limit"), 60)),
                )
                return
            if path == "/gaps":
                self._json(HTTPStatus.OK, service.gaps(_int(parameters.get("limit"), 50)))
                return
            if path == "/pages":
                self._json(HTTPStatus.OK, service.pages())
                return
            if path.startswith("/pages/"):
                self._json(HTTPStatus.OK, service.page(unquote(path[len("/pages/") :])))
                return
            if path == "/search":
                self._json(
                    HTTPStatus.OK,
                    service.search(
                        parameters.get("q", ""),
                        filters={
                            "admission": parameters.get("admission"),
                            "material_kind": parameters.get("kind"),
                            "status": parameters.get("status"),
                            "topic_key": parameters.get("topic"),
                        },
                        limit=_int(parameters.get("limit"), 10),
                    ),
                )
                return
            if path.startswith("/statement/"):
                self._json(HTTPStatus.OK, service.statement(unquote(path[len("/statement/") :])))
                return
            if path == "/prompts":
                self._json(
                    HTTPStatus.OK,
                    service.prompts(
                        count=_int(parameters.get("count"), PROMPTS_ON_WELCOME, ceiling=12),
                        **(
                            {"seed": _int(parameters.get("seed"), 0)}
                            if parameters.get("seed")
                            else {}
                        ),
                    ),
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "нет такого адреса", "path": path})

        def _client(self) -> str:
            """Who is asking, as well as a reverse proxy can say.

            Spoofable, and treated as such: this identifies a caller for a speed
            limit, never for a permission. The daily cap is what holds when the
            header lies.
            """
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return forwarded or self.client_address[0]

        def _validate_key(self) -> None:
            """A key says what it opens, and nothing else.

            Throttled apart from the question window: checking a key must not
            spend anybody's conversation allowance, and must not run free either.
            The answer carries no hint beyond valid/plan/expiry - a probe learns
            one bit, at its own cost.
            """
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type != "application/json":
                self._json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": "ожидается Content-Type: application/json"},
                )
                return
            if service.validate_budget.refused(self._client()) is not None:
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "слишком много проверок"})
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "тело запроса не JSON"})
                return
            key = str(payload.get("key", "") if isinstance(payload, dict) else "")
            checked = service.guard.check(f"Bearer {key.strip()}" if key else None)
            self._json(
                HTTPStatus.OK,
                {
                    "valid": bool(checked["valid"]),
                    "plan": checked["plan"],
                    "expiresAt": checked["expiresAt"],
                },
            )

        def _sse(self, event: str, payload: Any) -> None:
            """One server-sent event, flushed, so the conveyor is seen live.

            The draft is never streamed: stages are facts the code established,
            and the answer event carries only what survived verification.
            """
            body = json.dumps(payload, ensure_ascii=False, default=str)
            self.wfile.write(f"event: {event}\ndata: {body}\n\n".encode())
            self.wfile.flush()

        def do_POST(self) -> None:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/access/validate":
                self._validate_key()
                return
            if path not in ("/ask", "/chat", "/chat/stream"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "нет такого адреса", "path": path})
                return
            # A JSON content type is what makes a cross-origin POST ask the
            # browser for permission first. Without this check the endpoint is a
            # form target: any page could spend this service's model budget with
            # its visitors' browsers, and never even see the answer.
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type != "application/json":
                self._json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": "ожидается Content-Type: application/json"},
                )
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "тело запроса не JSON"})
                return
            asked = payload if isinstance(payload, dict) else {}
            question = str(asked.get("question", ""))
            admission = str(asked.get("admission", "knowledge"))
            session = str(asked.get("session", ""))
            # A live key widens this client's conversation window. Free calls and
            # keyed calls share one window per client, so alternating keys does
            # not multiply anybody's reach.
            access = service.access(self.headers.get("Authorization"))
            asks = SUBSCRIBER_ASKS_PER_CLIENT if access["valid"] else None
            digest = access.get("digest") if access["valid"] else None
            if path == "/ask":
                try:
                    self._json(
                        HTTPStatus.OK,
                        service.ask(
                            question,
                            client=self._client(),
                            admission=admission,
                            asks_per_client=asks,
                            key_digest=digest,
                        ),
                    )
                except OrchestratorError:
                    # The model was unreachable or refused. That is not an answer and
                    # must not look like one - and the reason belongs in the journal,
                    # not in a public response: it carries the provider's own status
                    # line and body fragments.
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE, {"error": "модель сейчас недоступна"}
                    )
                except Exception:
                    # Anything else - most likely `parse_answer` on a reply that is not
                    # the JSON the protocol asked for. Before this, such a reply raised
                    # through `BaseHTTPRequestHandler`, which logs the traceback and
                    # closes the socket: the caller got no status line at all, and the
                    # site showed a network error rather than a failure. A public
                    # endpoint owes an answer even when the answer is "no".
                    # `log_message` is deliberately silent, so the traceback goes
                    # straight to stderr, which is what systemd journals.
                    sys.stderr.write(f"ask failed:\n{traceback.format_exc()}")
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "ответ модели не удалось разобрать"},
                    )
                return
            if path == "/chat":
                try:
                    _, answered = service.chat(
                        question,
                        client=self._client(),
                        admission=admission,
                        session=session,
                        asks_per_client=asks,
                        key_digest=digest,
                    )
                    self._json(
                        HTTPStatus.BAD_REQUEST if "error" in answered else HTTPStatus.OK,
                        answered,
                    )
                except OrchestratorError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE, {"error": "модель сейчас недоступна"}
                    )
                except Exception:
                    sys.stderr.write(f"chat failed:\n{traceback.format_exc()}")
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "ответ модели не удалось разобрать"},
                    )
                return
            # /chat/stream: the same turn, framed as server-sent events
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for event, payload in service.chat_events(
                    question,
                    client=self._client(),
                    admission=admission,
                    session=session,
                    asks_per_client=asks,
                    key_digest=digest,
                ):
                    self._sse(event, payload)
            except OrchestratorError:
                self._sse("error", {"error": "модель сейчас недоступна"})
            except Exception:
                sys.stderr.write(f"chat stream failed:\n{traceback.format_exc()}")
                self._sse("error", {"error": "ответ модели не удалось разобрать"})

        def log_message(self, format: str, *args: Any) -> None:
            """Silence. systemd already timestamps, and a URL carries a question."""

    return Handler


def _int(value: str | None, default: int, *, ceiling: int = MAX_HITS) -> int:
    """A number from a URL, kept between one and a ceiling.

    The first version capped only the bottom, so `?limit=99999999` returned a
    whole table in one response. A limit a caller can raise without bound is not
    a limit.
    """
    try:
        return max(1, min(ceiling, int(value or default)))
    except ValueError:
        return default


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 19703,
    server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer,
) -> ThreadingHTTPServer:
    service = AgentService(Database(settings), settings)
    return server_factory((host, port), make_handler(service))
