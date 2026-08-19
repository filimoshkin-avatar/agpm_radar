"""Durable fail-closed publisher state machine backed by the external audit journal."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from packages.publisher.audit_journal import AuditEvent, append_event, read_and_verify

INITIAL_STATE: Final = "RECEIVED"
TERMINAL_STATES: Final = frozenset(
    {"SUCCEEDED", "REJECTED", "FAILED_PRE_ACTIVATION", "ROLLED_BACK", "NEEDS_RECONCILIATION"}
)
BLOCKING_STATE: Final = "NEEDS_RECONCILIATION"
TRANSITIONS: Final = {
    ("RECEIVED", "validation_passed"): "VALIDATED",
    ("RECEIVED", "validation_rejected"): "REJECTED",
    ("VALIDATED", "source_staging_passed"): "SOURCE_STAGED",
    ("VALIDATED", "source_staging_failed"): "FAILED_PRE_ACTIVATION",
    ("SOURCE_STAGED", "artifact_build_passed"): "ARTIFACTS_BUILT",
    ("SOURCE_STAGED", "artifact_build_failed"): "FAILED_PRE_ACTIVATION",
    ("ARTIFACTS_BUILT", "delta_build_passed"): "DELTA_BUILT",
    ("ARTIFACTS_BUILT", "delta_build_failed"): "FAILED_PRE_ACTIVATION",
    ("DELTA_BUILT", "transport_and_remote_stage_passed"): "REMOTE_STAGED",
    ("DELTA_BUILT", "transport_or_remote_stage_failed"): "FAILED_PRE_ACTIVATION",
    ("REMOTE_STAGED", "remote_verification_passed"): "REMOTE_VERIFIED",
    ("REMOTE_STAGED", "remote_verification_failed"): "FAILED_PRE_ACTIVATION",
    ("REMOTE_VERIFIED", "remote_pointer_activated"): "REMOTE_ACTIVE",
    ("REMOTE_ACTIVE", "api_connections_reopened"): "API_REOPENED",
    ("REMOTE_ACTIVE", "api_reopen_failed"): "FAILED_POST_REMOTE_ACTIVATION",
    ("API_REOPENED", "loopback_release_and_hash_verified"): "LOOPBACK_VERIFIED",
    ("API_REOPENED", "loopback_verification_failed"): "FAILED_POST_REMOTE_ACTIVATION",
    ("LOOPBACK_VERIFIED", "public_release_and_hash_verified"): "PUBLIC_VERIFIED",
    ("LOOPBACK_VERIFIED", "public_verification_failed"): "FAILED_POST_REMOTE_ACTIVATION",
    ("PUBLIC_VERIFIED", "source_pointer_committed"): "SOURCE_COMMITTED",
    ("PUBLIC_VERIFIED", "source_commit_failed"): "FAILED_POST_REMOTE_ACTIVATION",
    ("SOURCE_COMMITTED", "result_persisted"): "SUCCEEDED",
    ("FAILED_POST_REMOTE_ACTIVATION", "previous_pointer_and_hash_verified"): "ROLLED_BACK",
    ("FAILED_POST_REMOTE_ACTIVATION", "rollback_not_proven"): "NEEDS_RECONCILIATION",
}


class PublisherStateError(RuntimeError):
    """Publisher journal state is invalid, ambiguous, duplicated or blocked."""


@dataclass(frozen=True, slots=True)
class CandidateState:
    """Verified durable state for one candidate."""

    candidate_id: str
    state: str | None
    events: int

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def _event_id(candidate_id: str, state: str) -> str:
    digest = hashlib.sha256(f"{candidate_id}:{state}".encode()).hexdigest()[:32]
    return f"evt_{digest}"


class PublisherStateMachine:
    """Validate and append exactly the accepted Stage 1 transition graph."""

    def __init__(self, journal_path: Path) -> None:
        self.journal_path = journal_path

    def _candidate_records(self, candidate_id: str) -> tuple[dict[str, object], ...]:
        return tuple(
            record
            for record in read_and_verify(self.journal_path)
            if record.get("candidate_id") == candidate_id
        )

    def _assert_identity(
        self,
        candidate_id: str,
        *,
        release_id: str,
        before_state_hash: str,
        after_state_hash: str,
    ) -> None:
        expected = (release_id, before_state_hash, after_state_hash)
        for record in self._candidate_records(candidate_id):
            actual = (
                record.get("release_id"),
                record.get("before_state_hash"),
                record.get("after_state_hash"),
            )
            if actual != expected:
                raise PublisherStateError("candidate journal release/hash identity differs")

    def state(self, candidate_id: str) -> CandidateState:
        records = self._candidate_records(candidate_id)
        if not records:
            return CandidateState(candidate_id, None, 0)
        first = records[0]
        if first.get("action") != "received" or first.get("result") != INITIAL_STATE:
            raise PublisherStateError("candidate journal does not begin at RECEIVED")
        identity = (
            first.get("release_id"),
            first.get("before_state_hash"),
            first.get("after_state_hash"),
        )
        if any(
            (
                record.get("release_id"),
                record.get("before_state_hash"),
                record.get("after_state_hash"),
            )
            != identity
            for record in records[1:]
        ):
            raise PublisherStateError("candidate journal contains mixed release/hash identities")
        current = INITIAL_STATE
        for record in records[1:]:
            event = record.get("action")
            target = record.get("result")
            if not isinstance(event, str) or not isinstance(target, str):
                raise PublisherStateError("candidate journal state/event is not text")
            expected = TRANSITIONS.get((current, event))
            if expected != target:
                raise PublisherStateError(
                    f"candidate journal transition is invalid: {current} --{event}--> {target}"
                )
            current = target
        return CandidateState(candidate_id, current, len(records))

    def publishing_blocked(self) -> bool:
        return any(
            record.get("result") == BLOCKING_STATE for record in read_and_verify(self.journal_path)
        )

    def receive(
        self,
        *,
        candidate_id: str,
        release_id: str,
        occurred_at: str,
        before_state_hash: str,
        after_state_hash: str,
    ) -> CandidateState:
        current = self.state(candidate_id)
        if current.state is not None:
            self._assert_identity(
                candidate_id,
                release_id=release_id,
                before_state_hash=before_state_hash,
                after_state_hash=after_state_hash,
            )
            return current
        if self.publishing_blocked():
            raise PublisherStateError("publisher journal is blocked by NEEDS_RECONCILIATION")
        append_event(
            self.journal_path,
            AuditEvent(
                event_id=_event_id(candidate_id, INITIAL_STATE),
                occurred_at=occurred_at,
                actor_id="local-publisher",
                action="received",
                release_id=release_id,
                candidate_id=candidate_id,
                before_state_hash=before_state_hash,
                after_state_hash=after_state_hash,
                result=INITIAL_STATE,
            ),
        )
        return self.state(candidate_id)

    def transition(
        self,
        *,
        candidate_id: str,
        release_id: str,
        event: str,
        occurred_at: str,
        before_state_hash: str,
        after_state_hash: str,
        reason: str | None = None,
    ) -> CandidateState:
        current = self.state(candidate_id)
        if current.state is None:
            raise PublisherStateError("candidate has not been received")
        self._assert_identity(
            candidate_id,
            release_id=release_id,
            before_state_hash=before_state_hash,
            after_state_hash=after_state_hash,
        )
        target = TRANSITIONS.get((current.state, event))
        if target is None:
            raise PublisherStateError(f"event {event} is invalid from {current.state}")
        append_event(
            self.journal_path,
            AuditEvent(
                event_id=_event_id(candidate_id, target),
                occurred_at=occurred_at,
                actor_id="local-publisher",
                action=event,
                release_id=release_id,
                candidate_id=candidate_id,
                before_state_hash=before_state_hash,
                after_state_hash=after_state_hash,
                result=target,
                reason=reason,
            ),
        )
        return self.state(candidate_id)


__all__ = [
    "BLOCKING_STATE",
    "INITIAL_STATE",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "CandidateState",
    "PublisherStateError",
    "PublisherStateMachine",
]
