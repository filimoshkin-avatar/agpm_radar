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
free text, marked "машинный ответ, не редакция базы", with the quotations under
it. Decision 9: the chat is a chat - questions and answers are kept for analysis,
there is no permanent address for an answer and no public retraction procedure,
because nothing here is published under the base's name.

A question is data, not an instruction (ADR-0005 §15), and so is every quotation
inside an evidence package.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from radar_kx.config import Settings
from radar_kx.database import Database
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

#: Longest question accepted. A question is one question; a page of text pasted
#: into the box is a way to spend the model budget, not a way to ask.
MAX_QUESTION_CHARS = 500

#: How many hits a search returns at most. The reader is looking for evidence,
#: not browsing a corpus.
MAX_HITS = 50

#: The two sentences that travel with every generated answer. The owner's wording
#: (decision 6), kept here as a constant rather than in a template, so no client
#: can render an answer without them.
MACHINE_NOTICE = "Машинный ответ, не редакция базы."
SIGNATURE = "AgPM Radar, машинная сборка"

#: The licence line the base carries (decision 10). Attribution for the
#: reworking and the structure; a quotation stays its rightholder's and always
#: travels with its link.
LICENCE = (
    "Переработка и структура — свободно со ссылкой на AgPM Radar. "
    "Цитаты остаются за правообладателями и приводятся со ссылкой на источник."
)


class AgentService:
    """What the agent mode may ask for. Reads; the only writes are its own log."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        #: Loaded once on the first question and kept. Loading e5 takes seconds;
        #: doing it per question would make the semantic arm the slow part.
        self._model: Any = None

    # -- level 4: the backbone and the shelves ---------------------------------

    def topics(self) -> dict[str, Any]:
        return {"topics": self.database.agent_topics(), "signature": SIGNATURE}

    def concept(self, topic_key: str) -> dict[str, Any]:
        """One card of the backbone: what the base holds under this subject."""
        card = self.database.agent_concept(topic_key)
        if card is None:
            return {"error": "нет такой темы", "topicKey": topic_key}
        return {**card, "signature": SIGNATURE, "licence": LICENCE}

    def observatory(
        self, *, since: str | None, until: str | None, kind: str | None
    ) -> dict[str, Any]:
        """A cut by class of event over a period - decision 4, not a feed."""
        return {
            "observatory": self.database.agent_observatory(since=since, until=until, kind=kind),
            "signature": SIGNATURE,
            "licence": LICENCE,
        }

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
        hits = self.database.agent_search(
            question[:MAX_QUESTION_CHARS], filters=filters, limit=min(limit, MAX_HITS)
        )
        return {"query": question[:MAX_QUESTION_CHARS], "hits": hits, "licence": LICENCE}

    def ask(self, question: str) -> dict[str, Any]:
        """The agent's own answer, marked as machine-written, with its evidence under it.

        The order is the owner's, not the model's: the base retrieves, the model
        drafts clauses and says which numbered quotation each rests on, and code
        checks that claim against the spans before anything is returned. A draft
        that does not survive the check becomes a refusal (ADR-0004 §9) rather
        than a hedged sentence, and what the base does hold nearby is returned
        beside it as its own field.
        """
        question = question.strip()[:MAX_QUESTION_CHARS]
        if not question:
            return {"error": "пустой вопрос"}

        cached = self.database.cached_answer(question, scope="public")
        if cached is not None:
            return self._as_answer(
                question,
                answer_text=cached.get("answer_text"),
                refusal_reason=cached.get("refusal_reason"),
                package=cached.get("evidence_package") or [],
                verification=cached.get("verification"),
                from_cache=True,
            )

        hits = self.database.agent_search(
            question,
            filters={"admission": "knowledge"},
            limit=PACKAGE_SIZE,
            question_vector=self._vector(question),
        )
        package = build_package(hits, size=PACKAGE_SIZE)
        if not package:
            refusal = refuse("no_evidence", "в базе нет подходящих подтверждений")
            self.database.record_answer(
                question=question,
                scope="public",
                mode="strict",
                package=(),
                refusal=refusal,
                answered_by="radar-kb-agent",
            )
            return self._as_answer(question, refusal_reason=refusal.reason, package=[])

        gateway = ModelGateway(self.database, self.settings)
        result = gateway.run(RESEARCH_ANSWER, build_answer_prompt(question, package))
        clauses = parse_research_answer(result.content)
        checked = verify(clauses, package, mode="strict")
        if not clauses or not checked.passes:
            refusal = refuse(
                "no_evidence",
                "черновик не прошёл проверку по цитатам" if clauses else "евидентной базы нет",
                package,
            )
            self.database.record_answer(
                question=question,
                scope="public",
                mode="strict",
                package=package,
                refusal=refusal,
                verification=checked,
                model=RESEARCH_ANSWER.model,
                answered_by="radar-kb-agent",
            )
            return self._as_answer(
                question,
                refusal_reason=refusal.reason,
                package=[element.as_json() for element in package],
                verification=checked.as_json(),
            )

        answer_text = render(clauses)
        self.database.record_answer(
            question=question,
            scope="public",
            mode="strict",
            package=package,
            answer_text=answer_text,
            verification=checked,
            model=RESEARCH_ANSWER.model,
            answered_by="radar-kb-agent",
        )
        return self._as_answer(
            question,
            answer_text=answer_text,
            package=[element.as_json() for element in package],
            verification=checked.as_json(),
        )

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
            if self._model is None:
                self._model = load_model()
            return to_pgvector(encode(self._model, [question], is_query=True)[0])
        except Exception:  # pragma: no cover - a missing model is not a failed answer
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
        return {**found, "licence": LICENCE}


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
            path = urlsplit(self.path).path.rstrip("/") or "/"
            parameters = _query(self.path)
            if path in ("/", "/health"):
                self._json(HTTPStatus.OK, {"service": "radar-kb", "status": "ok"})
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
                    ),
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
            self._json(HTTPStatus.NOT_FOUND, {"error": "нет такого адреса", "path": path})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path != "/ask":
                self._json(HTTPStatus.NOT_FOUND, {"error": "нет такого адреса", "path": path})
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "тело запроса не JSON"})
                return
            question = str(payload.get("question", "")) if isinstance(payload, dict) else ""
            try:
                self._json(HTTPStatus.OK, service.ask(question))
            except OrchestratorError as error:
                # The model was unreachable or refused. That is not an answer and
                # must not look like one.
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "модель недоступна", "detail": str(error)[:200]},
                )

        def log_message(self, format: str, *args: Any) -> None:
            """Silence. systemd already timestamps, and a URL carries a question."""

    return Handler


def _int(value: str | None, default: int) -> int:
    try:
        return max(1, int(value or default))
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
