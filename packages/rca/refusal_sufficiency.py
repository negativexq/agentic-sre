"""m21 §36: a dependency question answered by a symptom met by refusals at another peer.

``m21.frontier.dependency-refusal-sufficient`` (v1). A firing alert that counts a Service's ``5xx`` responses is met,
at every evaluation that made it fire, by requests refused at another peer before they reached the dependency, and
that peer does not use the dependency. The dependency then cannot be the alert's initiating cause. The fact is a
lower bound: a record the capture missed can only add requests (unlike §31's universal claim, withdrawn in §31.7).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timedelta

from packages.rca.evidence_coverage import TraceRead
from packages.rca.frontier import DEPENDENCY_REFUSAL_SUFFICIENT_RULE, dependency_declared_unused
from packages.rca.model import (
    EntityRef,
    ExecutionOperation,
    ExecutionRecordCapture,
    FrontierAnswer,
    ObjectVersion,
    StructuralAlternative,
    SymptomCapture,
    TraceSpanObservation,
)
from packages.rca.topology import WORKLOAD_KINDS, declared_hosts

ANSWER = "ANSWERED_SYMPTOM_MET_ELSEWHERE"
QUESTION = "IS_THE_DEPENDENCY_AN_UNKNOWN_UPSTREAM_ROLE"
# the server request counter incremented with each terminal record: a measurement binding fixed by §36.3, not a name
REQUEST_COUNTER = "http_requests_total"
# a refused connection, as the workload's instrumentation names it (error.type)
REFUSALS = frozenset({"ConnectionRefusedError", "ECONNREFUSED"})

_FORM = re.compile(
    r"^\s*sum\s*\(\s*rate\s*\(\s*(?P<counter>[a-zA-Z_:][a-zA-Z0-9_:]*)\s*\{(?P<selector>[^{}]*)\}"
    r"\s*\[\s*(?P<range>\d+)(?P<unit>[sm])\s*\]\s*\)\s*\)\s*(?P<op>>=|>)\s*(?P<threshold>[0-9]*\.?[0-9]+)\s*$"
)
_MATCHER = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*')


class Form:
    """The recognized expression: ``sum(rate(C{service="S",status=~"5.."}[R])) > T``."""

    def __init__(self, service: str, range_seconds: float, threshold: float, inclusive: bool):
        self.service = service
        self.range_seconds = range_seconds
        self.threshold = threshold
        self.inclusive = inclusive

    def met(self, count: int) -> bool:
        rate = count / self.range_seconds
        return rate >= self.threshold if self.inclusive else rate > self.threshold


def _only_server_errors(pattern: str) -> bool:
    try:
        compiled = re.compile(pattern.encode().decode("unicode_escape"))
    except re.error:
        return False
    return all(
        bool(compiled.fullmatch(str(code))) == (500 <= code <= 599) for code in range(100, 600)
    )


def recognize(expression: str | None) -> Form | None:
    """The §36.3 condition 2 form of an alert expression, or None for any other form."""
    match = _FORM.match(expression or "")
    if match is None or match["counter"] != REQUEST_COUNTER:
        return None
    selector = match["selector"].strip().rstrip(",")
    matchers: dict[str, tuple[str, str]] = {}
    for part in filter(None, (item.strip() for item in selector.split(","))):
        found = _MATCHER.fullmatch(part)
        if found is None or found[1] in matchers:
            return None
        matchers[found[1]] = (found[2], found[3])
    if set(matchers) != {"service", "status"} or matchers["service"][0] != "=":
        return None
    operator, pattern = matchers["status"]
    if operator != "=~" or not _only_server_errors(pattern):
        return None
    seconds = int(match["range"]) * (60 if match["unit"] == "m" else 1)
    return Form(
        matchers["service"][1], float(seconds), float(match["threshold"]), match["op"] == ">="
    )


def _workloads(
    service: EntityRef,
    selects: Mapping[EntityRef, Collection[EntityRef]],
    owned_by: Mapping[EntityRef, EntityRef],
) -> set[EntityRef]:
    found: set[EntityRef] = set()
    for pod in selects.get(service, ()):
        owner = owned_by.get(pod)
        while owner is not None and owner.kind not in WORKLOAD_KINDS:
            owner = owned_by.get(owner)
        if owner is not None:
            found.add(owner)
    return found


def _ticks(fired_at: datetime, for_seconds: float, interval_seconds: float) -> list[datetime]:
    """Every evaluation from the pending start to the firing, the firing included."""
    steps = math.floor(for_seconds / interval_seconds + 1e-9)
    return [fired_at - timedelta(seconds=interval_seconds * k) for k in range(steps + 1)]


def dependency_refusal_sufficient(
    alternative: StructuralAlternative,
    captures: Sequence[SymptomCapture],
    latest: Mapping[EntityRef, ObjectVersion],
    selects: Mapping[EntityRef, Collection[EntityRef]],
    owned_by: Mapping[EntityRef, EntityRef],
    spans: Sequence[TraceSpanObservation],
    reads: Sequence[TraceRead] | None,
    onset: datetime | None,
) -> FrontierAnswer | None:
    """m21 §36. None when the rule does not apply: not a dependency question, or no capture of an alert of the
    callers' Service (every run captured before 2.15.0). Otherwise the answer, ``OPEN`` with the failed condition."""
    if alternative.role != "dependency":
        return None
    callers = {entity for entity in alternative.affected_entities if entity.kind in WORKLOAD_KINDS}
    if not callers:
        return None
    bound = [
        capture
        for capture in captures
        if capture.rule.service is not None
        and capture.rule.namespace is not None
        and callers
        <= _workloads(
            EntityRef(namespace=capture.rule.namespace, kind="Service", name=capture.rule.service),
            selects,
            owned_by,
        )
    ]
    if not bound:
        return None
    dependency = alternative.actor
    reasons: list[tuple[str, datetime | None]] = []
    for capture in bound:
        reason, evidence, failed_at = _assess(
            capture, callers, dependency, latest, selects, owned_by, spans, reads, onset
        )
        if reason is None:
            return FrontierAnswer(
                alternative_id=alternative.alternative_id,
                question=QUESTION,
                state=ANSWER,
                evidence_ids=tuple(sorted(evidence)),
                affected_claims=alternative.material_for_hypothesis_ids,
                remaining_uncertainty=("AN_ADDITIONAL_CONTRIBUTION", "THE_PEER_S_OWN_CAUSE"),
                rule_id=DEPENDENCY_REFUSAL_SUFFICIENT_RULE,
            )
        reasons.append((reason, failed_at))
    reason, failed_at = sorted(reasons, key=lambda item: item[0])[0]
    return FrontierAnswer(
        alternative_id=alternative.alternative_id,
        question=QUESTION,
        state="OPEN",
        blocked_reason=reason,
        failed_evaluation_at=failed_at,
        affected_claims=alternative.material_for_hypothesis_ids,
        remaining_uncertainty=("UPSTREAM_ROLE_UNDETERMINED",),
        rule_id=DEPENDENCY_REFUSAL_SUFFICIENT_RULE,
    )


def _assess(
    capture: SymptomCapture,
    callers: set[EntityRef],
    dependency: EntityRef,
    latest: Mapping[EntityRef, ObjectVersion],
    selects: Mapping[EntityRef, Collection[EntityRef]],
    owned_by: Mapping[EntityRef, EntityRef],
    spans: Sequence[TraceSpanObservation],
    reads: Sequence[TraceRead] | None,
    onset: datetime | None,
) -> tuple[str | None, set[str], datetime | None]:
    rule, execution = capture.rule, capture.execution
    # 1. captured: the rule first, since the ExecutionRecord is read only for a rule of the form
    if rule.failed:
        return "NOT_CAPTURED", set(), None
    # 2. a counted request outcome, bound to the alert's own Service
    form = recognize(rule.expression)
    if form is None or form.service != rule.service:
        return "FORM_UNSUPPORTED", set(), None
    # 3. known timing
    if not rule.for_seconds or not rule.interval_seconds or rule.scrape_interval_seconds is None:
        return "TIMING_UNKNOWN", set(), None
    if execution is None or execution.failed:
        return "NOT_CAPTURED", set(), None
    # 4 and 5. requests refused at a peer that does not use the dependency, before reaching it
    services = [ref for ref in latest if ref.kind == "Service" and ref != dependency]
    to_dependency = set().union(*(declared_hosts(latest, caller, dependency) for caller in callers))
    peers = {
        host: service
        for service in services
        for caller in callers
        for host in declared_hosts(latest, caller, service)
    }
    allowed: dict[EntityRef, bool] = {}

    def peer_free(peer: EntityRef) -> bool:
        if peer not in allowed:
            workloads = _workloads(peer, selects, owned_by)
            allowed[peer] = bool(workloads) and all(
                not declared_hosts(latest, workload, dependency)
                or (
                    (
                        answer := dependency_declared_unused(
                            StructuralAlternative(
                                alternative_id=f"{DEPENDENCY_REFUSAL_SUFFICIENT_RULE}:peer",
                                actor=dependency,
                                role="dependency",
                                affected_entities=(workload,),
                            ),
                            latest,
                            spans,
                            reads,
                            onset,
                        )
                    )
                    is not None
                    and answer.state == "ANSWERED_DECLARED_UNUSED"
                )
                for workload in workloads
            )
        return allowed[peer]

    counted, refused_elsewhere = _counted(execution, peers, to_dependency, peer_free)
    if refused_elsewhere and not counted:
        return "PEER_USES_IT", set(), None
    # 6. enough at every evaluation
    range_ = timedelta(seconds=form.range_seconds)
    scrape = timedelta(seconds=rule.scrape_interval_seconds)
    for tick in sorted(_ticks(rule.fired_at, rule.for_seconds, rule.interval_seconds)):
        n = sum(1 for at in counted if tick - range_ + scrape <= at <= tick - scrape)
        if not form.met(n):
            return "NOT_SUFFICIENT", set(), tick
    return None, {rule.evidence_id, execution.evidence_id}, None


def _counted(
    execution: ExecutionRecordCapture,
    peers: Mapping[str, EntityRef],
    to_dependency: set[str],
    peer_free: Callable[[EntityRef], bool],
) -> tuple[list[datetime], bool]:
    """The terminal times of the requests condition 4 and 5 count, and whether any refusal at a peer was set aside."""
    operations: dict[str, list[ExecutionOperation]] = {}
    for operation in execution.operations:
        operations.setdefault(operation.trace_id, []).append(operation)
    counted: list[datetime] = []
    set_aside = False
    for terminal in execution.terminals:
        if not 500 <= terminal.status <= 599:
            continue
        own = sorted(operations.get(terminal.trace_id, ()), key=lambda o: o.started_at)
        failed = [o for o in own if o.outcome == "ERROR"]
        if not failed:
            continue
        last = failed[-1]
        if (
            last.error_type not in REFUSALS
            or last.server_address is None
            or last.server_address not in peers
        ):
            continue
        if any(o.server_address in to_dependency for o in own if o.started_at <= last.started_at):
            continue
        if not peer_free(peers[last.server_address]):
            set_aside = True
            continue
        counted.append(terminal.at)
    return counted, set_aside


__all__ = [
    "ANSWER",
    "DEPENDENCY_REFUSAL_SUFFICIENT_RULE",
    "dependency_refusal_sufficient",
    "recognize",
]
