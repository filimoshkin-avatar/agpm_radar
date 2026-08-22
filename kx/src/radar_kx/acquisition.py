"""The escalation ladder: a failure is a routing signal, not an ending (slice 2.3).

Full text exists for 5 979 of 8 313 documents. The 2 334 that do not are not one
problem and must not share one outcome. 1 745 are Reddit refusing a
robots-respecting client, some are pages that only yield text once rendered, some
are gone from the web and present in an archive, and some genuinely have no public
text at all. Today all of them are `failed`, which says what happened and nothing
about what to do.

This module holds the three pieces of judgement:

* **the ladder** - which rungs exist and in what order;
* **escalation** - which failure means "try the next rung" and which means "stop,
  and here is what we now believe";
* **the host profile** - which rungs are worth trying on a given host, at what
  pace, with which headers, and whether robots is a routing signal or a wall.

The default profile reproduces today's behaviour exactly: one rung, respect
robots, the global pace. Everything here is inert until somebody writes a profile
and says why. That is deliberate - how we treat somebody else's server should be a
decision that was made, not a deployment that happened.

Owner decision P11 made robots a routing signal rather than a terminal state, and
made it a **product** decision rather than a legal conclusion. The grain matters:
`RADAR_KX_RESPECT_ROBOTS` is global today, and flipping it would change how the
crawler behaves towards every host at once. A profile changes one host, records
who decided it, and requires a reason long enough to be a reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: Every rung, in the order the plan gives them (§11.6). A rung is a way of
#: obtaining text, and `document_versions.source_kind` records which one worked.
LADDER = (
    "network",
    "network_browser_headers",
    "network_robots_override",
    "source_specific_parse",
    "browser_render",
    "web_archive",
    "operator_artifact",
)

#: What the fetcher can do on its own. `operator_artifact` is a person handing us
#: a file, and `browser_render` needs a unit that does not exist yet (defect D7,
#: ADR-0005 §11: built by measured need, when the gap queue shows cases only a
#: browser can obtain).
AUTOMATIC_RUNGS = ("network", "network_browser_headers", "network_robots_override")

#: The default ladder for a host nobody has written a profile for: exactly what
#: the fetcher does today, and no more.
DEFAULT_RUNGS = ("network",)

#: A terminal state says what we now believe about the document. It is a different
#: question from `fetch_queue.status`, which says what happened on the last try.
TERMINAL_REASONS = (
    "obtained",
    "removed_at_source",
    "requires_credentials",
    "no_public_text",
    "blocked_by_host",
    "ladder_exhausted",
    "refused_by_policy",
    "transient_exhausted",
)

#: Failures that end the ladder, with what they mean and whose move it is. Anything
#: not here is worth another rung.
TERMINAL_BY_ERROR: dict[str, tuple[str, str]] = {
    "http_404": ("removed_at_source", "machine"),
    "http_410": ("removed_at_source", "machine"),
    "http_401": ("requires_credentials", "owner"),
    "http_402": ("requires_credentials", "owner"),
    "http_407": ("requires_credentials", "owner"),
    "http_451": ("no_public_text", "owner"),
}

#: Failures that say nothing about the document. The attempts ran out; a requeue
#: is the whole action, and nobody needs to look at the page. Reported separately
#: because filing 79 timeouts under "a person must decide" is how a gap queue
#: stops being read.
TRANSIENT_ERRORS = frozenset(
    {
        "timeout",
        "network_error",
        "parse_or_io_error",
        "empty_response",
        "redirect_without_location",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
    }
)

#: Which rung a failure suggests trying next, when the profile allows it. A 403 or
#: a 429 is a host declining this client, not declining the document.
#: Verified against the codes the fetcher actually emits and the counts production
#: actually holds, rather than against a guess: robots_denied 1 876, http_403 294,
#: weak_or_missing_text 86, http_429 40. The first version of this table named
#: `parser_no_text` and `empty_body`, neither of which exists, so two of its five
#: rules could never have fired.
ESCALATION_HINT: dict[str, str] = {
    "robots_denied": "network_robots_override",
    "http_403": "network_browser_headers",
    "http_429": "network_browser_headers",
    "content_parse_error": "source_specific_parse",
    "weak_or_missing_text": "browser_render",
    "body_too_large": "web_archive",
    "too_many_redirects": "web_archive",
}


class AcquisitionError(ValueError):
    """A profile or a ladder state cannot be used."""


@dataclass(frozen=True, slots=True)
class HostProfile:
    """How one host is to be treated, and who decided that."""

    host: str
    rungs: tuple[str, ...] = DEFAULT_RUNGS
    min_interval_seconds: float | None = None
    max_in_flight: int | None = None
    robots_policy: str = "respect"
    request_headers: Mapping[str, str] = field(default_factory=dict)
    rationale: str = "default profile: today's behaviour, one rung, respect robots"
    decided_by: str = "default"

    def __post_init__(self) -> None:
        unknown = [rung for rung in self.rungs if rung not in LADDER]
        if unknown:
            raise AcquisitionError(f"{self.host}: unknown rungs {unknown}")
        if self.robots_policy not in {"respect", "override_recorded"}:
            raise AcquisitionError(f"{self.host}: unknown robots policy")

    @property
    def refuses_everything(self) -> bool:
        """An empty ladder is a decision, not an omission."""
        return not self.rungs

    def allows(self, rung: str) -> bool:
        if rung == "network_robots_override":
            return rung in self.rungs and self.robots_policy == "override_recorded"
        return rung in self.rungs

    def as_json(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "rungs": list(self.rungs),
            "minIntervalSeconds": self.min_interval_seconds,
            "maxInFlight": self.max_in_flight,
            "robotsPolicy": self.robots_policy,
            "requestHeaders": dict(self.request_headers),
            "rationale": self.rationale,
            "decidedBy": self.decided_by,
        }


def host_of(canonical_url: str) -> str:
    return (urlsplit(canonical_url).hostname or "").lower()


def profile_for(canonical_url: str, profiles: Mapping[str, HostProfile]) -> HostProfile:
    """The profile for a URL's host, or the default one.

    Exact host only. A profile for ``reddit.com`` deliberately does not cover
    ``old.reddit.com``: those are different servers with different rules, and
    inheriting a robots override down a domain tree is how one decision quietly
    becomes twenty.
    """
    host = host_of(canonical_url)
    return profiles.get(host, HostProfile(host=host))


@dataclass(frozen=True, slots=True)
class LadderStep:
    """What to do next with a document, and why."""

    rung: str | None
    terminal_reason: str | None
    next_action_owner: str | None
    detail: str

    @property
    def is_terminal(self) -> bool:
        return self.rung is None


def next_step(
    *,
    profile: HostProfile,
    tried: Sequence[str],
    error_code: str | None,
) -> LadderStep:
    """Decide the next rung, or stop with a reason somebody can act on."""
    if profile.refuses_everything:
        return LadderStep(None, "refused_by_policy", "owner", f"{profile.host} is not fetched")

    if error_code in TERMINAL_BY_ERROR:
        reason, owner = TERMINAL_BY_ERROR[error_code]
        return LadderStep(None, reason, owner, f"{error_code} ends the ladder")

    if error_code in TRANSIENT_ERRORS:
        return LadderStep(
            None,
            "transient_exhausted",
            "machine",
            f"{error_code} says nothing about the document; requeue is the action",
        )

    already = set(tried)
    hinted = ESCALATION_HINT.get(error_code or "")
    if hinted and hinted not in already:
        if profile.allows(hinted) and hinted in AUTOMATIC_RUNGS:
            return LadderStep(hinted, None, None, f"{error_code} suggests {hinted}")
        if profile.allows(hinted):
            # The right next rung exists and no machine here can climb it. That is
            # the measured need ADR-0005 §11 asks for before a browser unit is
            # built, and it belongs in the gap queue with a person's name on it.
            return LadderStep(
                None,
                "ladder_exhausted",
                "operator",
                f"{error_code} needs {hinted}, which needs a person",
            )
        if hinted == "network_robots_override":
            # The host said no and nobody has decided otherwise for this host.
            # That is a decision waiting to be made, not a dead end (P11).
            return LadderStep(
                None,
                "blocked_by_host",
                "owner",
                f"{profile.host} denies robots and has no recorded override",
            )
        if hinted in AUTOMATIC_RUNGS:
            # A rung the fetcher can climb and this host does not allow is one
            # decision away, not a page anybody needs to read. Measured on
            # production that is 324 documents: without this they read as
            # "somebody look at these" and what they are is "write a profile".
            return LadderStep(
                None,
                "blocked_by_host",
                "owner",
                f"{profile.host} would need {hinted}, and no profile allows it",
            )
        # A rung no machine here can climb, whatever the profile says. These rows
        # are the measured need ADR-0005 §11 asks for before the browser unit of
        # slice 2.3a is built: on production, 80 documents whose text only a
        # renderer gets.
        return LadderStep(
            None,
            "ladder_exhausted",
            "operator",
            f"{error_code} needs {hinted}, which needs a person",
        )

    for rung in profile.rungs:
        if rung in already:
            continue
        if not profile.allows(rung):
            continue
        if rung not in AUTOMATIC_RUNGS:
            # A rung the fetcher cannot climb is not a failure of the document.
            return LadderStep(None, "ladder_exhausted", "operator", f"{rung} needs a person")
        return LadderStep(rung, None, None, "next rung on this host's profile")

    return LadderStep(None, "ladder_exhausted", "operator", "every rung on this profile was tried")
