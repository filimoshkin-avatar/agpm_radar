"""The side of a model call that Radar owns.

ADR-0005 §6: the orchestrator has no internet. It reaches Hermes on loopback and
PostgreSQL on a unix socket, and the systemd unit is what makes that true rather
than this module's good intentions.

Two obligations of the egress contract (P18) live here as code rather than prose:

**Context minimization.** Every run type declares what it may send and the largest
payload it may send it in. :meth:`ModelGateway.run` refuses the call when a payload
exceeds the declaration. ADR-0005 §3 says the rule is fixed per run type and recorded
with it; a rule that is only recorded is a wish, so it is also enforced. The cap is a
character count because that is what the store measures and what an operator can
check against a chunk.

**Audit.** Every call writes one row to ``egress_audit`` (immutable, migration 003) -
including the calls that failed, and including the calls this module refused before
anything left the host. A refusal that leaves no trace is indistinguishable from a
call that was never attempted, and the difference between those two is exactly what
somebody reviewing the boundary needs to see.

The proxy keeps its own journal of the same calls, from the other side of the
boundary. The two records are not one-to-one and should not be read as if they were:
Hermes makes calls of its own that no run type asked for - the first production run
showed nine tunnels against three audited calls, the difference being model discovery
and two manual checks. What the pair supports is the one-way check that matters: an
audited call with no tunnel behind it did not happen the way the row says it did.

The two-model limit of P9 is checked here too, against :data:`ALLOWED_MODELS`. That is
the third place it is enforced - the Hermes profile config routes only these models,
the profile's entry point rejects any other identifier in a request body, and this
refuses to ask. Three layers for one rule is not redundancy by accident: the config is
a file on a host, the entry point is a patch that an upgrade could drop, and this is
the only one of the three that lives in a repository with gates.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from radar_kx.config import Settings
from radar_kx.database import Database
from radar_kx.extraction import (
    EXTRACTOR_VERSION,
    ExtractionError,
    Fragment,
    ProposedClaim,
    build_prompt,
    parse_answer,
)
from radar_kx.identifiers import sha256_bytes

#: Model alias to provider, exactly as the extraction profile's ``model_routes``
#: declares them (P9). Nothing else may be asked for.
ALLOWED_MODELS = {"glm-5.2": "zai", "MiniMax-M3": "minimax"}

DEFAULT_MODEL = "glm-5.2"

#: How many times a call waits out a busy profile before giving up. The extraction
#: profile refuses past ten concurrent runs, and a batch pass with eight workers
#: beside another one crosses that line in seconds. Three tries at a widening
#: interval covers a passing overlap; a profile that is busy for a minute is a
#: different problem and should surface as one.
BUSY_RETRIES = 3
BUSY_BACKOFF_SECONDS = 4.0

#: What "come back later" looks like coming out of the profile.
_BUSY_SIGNS = ("429", "rate_limit", "too many concurrent", "overloaded", "timed out", "timeout")


def _is_busy(detail: str) -> bool:
    lowered = detail.casefold()
    return any(sign in lowered for sign in _BUSY_SIGNS)


class OrchestratorError(RuntimeError):
    """The call cannot be made, or its answer cannot be used."""


@dataclass(frozen=True, slots=True)
class RunType:
    """One kind of model call, with the egress rule that belongs to it."""

    name: str
    #: Recorded in ``egress_audit.purpose``.
    purpose: str
    model: str
    #: What this run type is allowed to put in front of the model, in words. This is
    #: the ADR-0005 §3 rule; ``max_payload_chars`` is the part of it a machine can
    #: check.
    context_rule: str
    max_payload_chars: int

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "model": self.model,
            "provider": ALLOWED_MODELS[self.model],
            "contextRule": self.context_rule,
            "maxPayloadChars": self.max_payload_chars,
        }


REACHABILITY_PROBE = RunType(
    name="reachability_probe",
    purpose="egress chain reachability probe",
    model=DEFAULT_MODEL,
    context_rule=(
        "a fixed English sentence and nothing else. The probe exists to prove that "
        "orchestrator, profile, proxy and provider are all reachable and that the "
        "audit row is written; it must never carry stored text, so the cap is far "
        "below any real fragment."
    ),
    max_payload_chars=200,
)


CLAIM_EXTRACTION = RunType(
    name="claim_extraction",
    purpose="claim extraction from one fragment",
    model=DEFAULT_MODEL,
    context_rule=(
        "one chunk of one document version and a fixed instruction block - never the "
        "whole document. A chunk is capped at 4000 characters by the store, so the cap "
        "here is that plus the instructions and nothing more. Extraction does not need "
        "the document to read a fragment, and sending it would send other people's text "
        "that no claim will ever rest on."
    ),
    max_payload_chars=6000,
)


IDEA_STATEMENT = RunType(
    name="idea_statement",
    purpose="phrasing a candidate idea from its quotations",
    model=DEFAULT_MODEL,
    context_rule=(
        "the quotations of one candidate group and a fixed instruction block - never "
        "the documents they came from. The group is capped at twelve claims and a "
        "quotation at a few hundred characters, so the cap here is that. The model is "
        "phrasing what the evidence already says; it does not need the articles to do "
        "that, and sending them would send text no claim rests on."
    ),
    max_payload_chars=8000,
)


QUOTE_TRANSLATION = RunType(
    name="quote_translation",
    purpose="translating one published quotation",
    model=DEFAULT_MODEL,
    context_rule=(
        "one quotation and a fixed instruction block - never the document it came "
        "from, and never several quotations at once. P32 caps a published quotation "
        "at one paragraph, so the cap here is that plus the instructions. A "
        "translator that could see the article would be tempted to smooth the "
        "quotation towards it, and the invariant check exists because that is "
        "exactly what must not happen."
    ),
    max_payload_chars=3000,
)


RESEARCH_ANSWER = RunType(
    name="research_answer",
    purpose="drafting an answer from a numbered evidence package",
    model=DEFAULT_MODEL,
    context_rule=(
        "one question and at most eight numbered quotations, and nothing else - not "
        "the documents they came from and not the rest of the store. The model "
        "drafts clauses and says which evidence each rests on; whether it holds is "
        "decided here, in code, against the spans. Eight quotations at a paragraph "
        "each is the cap."
    ),
    max_payload_chars=14000,
)

TOPIC_ASSIGNMENT = RunType(
    name="topic_assignment",
    purpose="placing statements and documents on the authored backbone",
    model=DEFAULT_MODEL,
    context_rule=(
        "the rubricator as the instruction block - Radar's own writing, and the "
        "owner's - and a numbered list of at most twenty-five items: one wiki "
        "statement each, or one document's title and the first 300 characters of "
        "its text. Never a whole document. Placing a document on a backbone needs "
        "what it is about, and a lede says that; the rest is other people's text "
        "that no claim will rest on."
    ),
    max_payload_chars=14000,
)

CLAIM_READING = RunType(
    name="claim_reading",
    purpose="reading a batch of statements: kind, source, admission and subject",
    model=DEFAULT_MODEL,
    context_rule=(
        "the owner's rules and her rubricator as the instruction block, and at most "
        "ten statements - each one the normalised claim, its quotation capped at 400 "
        "characters, and the name of the corpus it came from. Never the document. "
        "Deciding what kind of material a sentence is needs the sentence and what it "
        "stands on; the article around it is other people's text that no claim will "
        "rest on."
    ),
    max_payload_chars=16000,
)

KNOWLEDGE_LINK = RunType(
    name="knowledge_link",
    purpose="judging what one statement does to another",
    model=DEFAULT_MODEL,
    context_rule=(
        "a fixed instruction block naming the four relation types, and at most "
        "twenty pairs of normalised statements capped at 300 characters each. "
        "Never a quotation and never a document: the judgement is about what two "
        "sentences say to each other, and the article around them is other "
        "people's text that no link will rest on."
    ),
    max_payload_chars=14000,
)

ENTITY_EXTRACTION = RunType(
    name="entity_extraction",
    purpose="naming the organisations, people, standards and roles a statement mentions",
    model=DEFAULT_MODEL,
    context_rule=(
        "a fixed instruction block naming the nine entity types and the two roles, "
        "and at most eight quotations capped at 400 characters each. Never the "
        "document: what a sentence names is in the sentence, and the article "
        "around it names other things that this statement does not."
    ),
    max_payload_chars=14000,
)

#: Every run type the orchestrator knows. Later slices add theirs here, and a run
#: type is not finished until its context rule is written (ADR-0005, consequences).
RUN_TYPES: dict[str, RunType] = {
    REACHABILITY_PROBE.name: REACHABILITY_PROBE,
    CLAIM_EXTRACTION.name: CLAIM_EXTRACTION,
    IDEA_STATEMENT.name: IDEA_STATEMENT,
    QUOTE_TRANSLATION.name: QUOTE_TRANSLATION,
    RESEARCH_ANSWER.name: RESEARCH_ANSWER,
    TOPIC_ASSIGNMENT.name: TOPIC_ASSIGNMENT,
    CLAIM_READING.name: CLAIM_READING,
    KNOWLEDGE_LINK.name: KNOWLEDGE_LINK,
    ENTITY_EXTRACTION.name: ENTITY_EXTRACTION,
}

PROBE_PROMPT = "Reply with the single word: ready."


@dataclass(frozen=True, slots=True)
class ModelResult:
    outcome: str
    content: str
    request_tokens: int | None
    response_tokens: int | None
    egress_id: int
    detail: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "content": self.content,
            "requestTokens": self.request_tokens,
            "responseTokens": self.response_tokens,
            "egressId": self.egress_id,
            "detail": self.detail,
        }


class ModelGateway:
    """Calls the extraction profile, and records what crossed the boundary."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def _post(self, model: str, payload: str, system: str | None) -> dict[str, Any]:
        if not self.settings.hermes_key:
            raise OrchestratorError("RADAR_KX_HERMES_KEY is not set")
        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": payload})
        response = httpx.post(
            f"{self.settings.hermes_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.hermes_key}"},
            json={"model": model, "messages": messages, "stream": False},
            timeout=self.settings.hermes_timeout_seconds,
            trust_env=False,
        )
        if response.status_code != 200:
            raise OrchestratorError(
                f"hermes returned {response.status_code}: {response.text[:400]}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise OrchestratorError("hermes returned a body that is not an object")
        return body

    @staticmethod
    def _content(body: dict[str, Any]) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OrchestratorError("hermes returned no choices")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OrchestratorError("hermes returned a choice with no text content")
        return content

    @staticmethod
    def _tokens(body: dict[str, Any]) -> tuple[int | None, int | None]:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None, None
        request = usage.get("prompt_tokens")
        response = usage.get("completion_tokens")
        return (
            int(request) if isinstance(request, int) else None,
            int(response) if isinstance(response, int) else None,
        )

    def run(
        self,
        run_type: RunType,
        payload: str,
        *,
        model: str | None = None,
        system: str | None = None,
        run_id: str | None = None,
        document_id: str | None = None,
        version_id: str | None = None,
        chunk_id: str | None = None,
    ) -> ModelResult:
        """Send one payload to one model, or refuse - and record either way."""
        chosen = model or run_type.model
        payload_sha = sha256_bytes(payload.encode("utf-8"))
        prompt_sha = sha256_bytes((system or "").encode("utf-8")) if system is not None else None

        def record(
            outcome: str,
            *,
            detail: str | None = None,
            request_tokens: int | None = None,
            response_tokens: int | None = None,
        ) -> int:
            return self.database.record_egress(
                provider=ALLOWED_MODELS.get(chosen, "unknown"),
                model=chosen,
                purpose=run_type.purpose,
                payload_chars=len(payload),
                payload_sha256=payload_sha,
                prompt_sha256=prompt_sha,
                outcome=outcome,
                error_detail=detail,
                run_id=run_id,
                document_id=document_id,
                version_id=version_id,
                chunk_id=chunk_id,
                request_tokens=request_tokens,
                response_tokens=response_tokens,
            )

        if chosen not in ALLOWED_MODELS:
            detail = f"{chosen!r} is not one of {sorted(ALLOWED_MODELS)}"
            raise OrchestratorError(f"{detail} (egress {record('refused_model', detail=detail)})")
        if len(payload) > run_type.max_payload_chars:
            detail = (
                f"{len(payload)} chars exceeds the {run_type.max_payload_chars} this run type "
                f"may send: {run_type.context_rule}"
            )
            raise OrchestratorError(
                f"{detail} (egress {record('refused_oversize_payload', detail=detail)})"
            )

        for attempt in range(BUSY_RETRIES + 1):
            try:
                body = self._post(chosen, payload, system)
                content = self._content(body)
                break
            except (httpx.HTTPError, OrchestratorError, json.JSONDecodeError) as exc:
                detail = f"{type(exc).__name__}: {exc}"[:2000]
                if attempt < BUSY_RETRIES and _is_busy(detail):
                    # The profile is at its concurrency ceiling, not broken. Waiting
                    # is the whole fix; failing here would silently drop a batch of
                    # work whose only problem was arriving at the same moment as
                    # another one. Every attempt is still audited.
                    record("failed", detail=detail)
                    time.sleep(BUSY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise OrchestratorError(
                    f"{detail} (egress {record('failed', detail=detail)})"
                ) from exc

        request_tokens, response_tokens = self._tokens(body)
        egress_id = record(
            "succeeded", request_tokens=request_tokens, response_tokens=response_tokens
        )
        return ModelResult(
            outcome="succeeded",
            content=content,
            request_tokens=request_tokens,
            response_tokens=response_tokens,
            egress_id=egress_id,
        )

    def probe(self, *, model: str | None = None) -> ModelResult:
        """Prove the whole chain end to end and leave a row saying so."""
        return self.run(REACHABILITY_PROBE, PROBE_PROMPT, model=model)


class HermesExtractor:
    """The first :class:`~radar_kx.extraction.ExtractionAdapter`, over the profile.

    It proposes and nothing else. Whether a proposal becomes evidence is decided
    by aligning its quotation against the store, in code, with no model involved.
    """

    def __init__(self, gateway: ModelGateway, *, model: str = DEFAULT_MODEL) -> None:
        self.gateway = gateway
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def propose(self, fragment: Fragment) -> tuple[ProposedClaim, ...]:
        result = self.gateway.run(
            CLAIM_EXTRACTION,
            build_prompt(fragment),
            model=self._model,
            version_id=fragment.version_id,
            chunk_id=fragment.chunk_id,
        )
        return parse_answer(result.content)

    @property
    def extractor_version(self) -> str:
        return EXTRACTOR_VERSION


__all__ = [
    "ALLOWED_MODELS",
    "CLAIM_EXTRACTION",
    "DEFAULT_MODEL",
    "IDEA_STATEMENT",
    "QUOTE_TRANSLATION",
    "REACHABILITY_PROBE",
    "RESEARCH_ANSWER",
    "RUN_TYPES",
    "ExtractionError",
    "HermesExtractor",
    "ModelGateway",
    "ModelResult",
    "OrchestratorError",
    "RunType",
]
