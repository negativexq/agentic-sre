"""Channel K reference coverage (M21 contract §4.5, amendment 5; audit only).

K is ``NO_PATH_COVERED`` only when K1–K6 are all positively satisfied and the
complete traversal finds no path. Every precondition is evaluated and recorded;
the final state follows the frozen precedence PATH > UNCOVERED > UNKNOWN >
NO_PATH_COVERED (§4.5.5). The rule is fail-closed by construction: every kind of
the v1 profile is INCOMPLETE, and no source supplies a journal-coverage fact or
Kubernetes audit evidence yet, so ``NO_PATH_COVERED`` cannot occur on today's
evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from packages.rca.json_access import child
from packages.rca.model import (
    CLUSTER_SCOPE,
    ChannelPrecondition,
    ChannelState,
    ClusterEvent,
    CoveragePreconditionStatus,
    Edge,
    EntityRef,
    ObjectVersion,
)
from packages.rca.topology import Topology, _config_refs, _pod_spec, derive_edges

RULE_ID = "m21.k-reference-coverage"
RULE_VERSION = "v1"

# §4.5.2 per-kind profile v1: kind -> (supported apiVersion, COMPLETE). Every kind
# starts INCOMPLETE; promotion is a versioned, owner-approved profile change.
KIND_PROFILE: dict[str, tuple[str, bool]] = {
    "Namespace": ("v1", False),
    "ConfigMap": ("v1", False),
    "Service": ("v1", False),
    "ServiceAccount": ("v1", False),
    "Job": ("batch/v1", False),
    "Pod": ("v1", False),
}
_RBAC_KINDS = frozenset({"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"})
# K1: the kinds a journal-coverage fact must name (profile, references, RBAC).
REQUIRED_JOURNAL_KINDS = frozenset(
    {
        *KIND_PROFILE,
        "ReplicaSet",
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "CronJob",
        "NetworkPolicy",
        *_RBAC_KINDS,
    }
)

# Versioned control-plane identity profile (§4.5.3): the Kubernetes bootstrap-policy
# identities only. An unrecognised ``system:*`` identity is not control plane.
CONTROL_PLANE_PROFILE = "m21.control-plane-identities.v1"
CONTROL_PLANE_USERS = frozenset(
    {"system:kube-controller-manager", "system:kube-scheduler", "system:apiserver"}
)
CONTROL_PLANE_SERVICE_ACCOUNTS = frozenset(
    f"system:serviceaccount:kube-system:{name}"
    for name in (
        "attachdetach-controller",
        "clusterrole-aggregation-controller",
        "cronjob-controller",
        "daemon-set-controller",
        "deployment-controller",
        "disruption-controller",
        "endpoint-controller",
        "endpointslice-controller",
        "endpointslicemirroring-controller",
        "ephemeral-volume-controller",
        "expand-controller",
        "generic-garbage-collector",
        "horizontal-pod-autoscaler",
        "job-controller",
        "legacy-service-account-token-cleaner",
        "namespace-controller",
        "node-controller",
        "persistent-volume-binder",
        "pod-garbage-collector",
        "pv-protection-controller",
        "pvc-protection-controller",
        "replicaset-controller",
        "replication-controller",
        "resourcequota-controller",
        "root-ca-cert-publisher",
        "route-controller",
        "service-account-controller",
        "service-controller",
        "statefulset-controller",
        "ttl-after-finished-controller",
        "ttl-controller",
    )
)

_READ_VERBS = frozenset({"get", "list", "watch", "*"})
_WRITE_VERBS = frozenset({"create", "update", "patch", "delete", "deletecollection", "*"})
_API_GROUP = {"Job": "batch"}
_RESOURCE = {
    "Namespace": "namespaces",
    "ConfigMap": "configmaps",
    "Service": "services",
    "ServiceAccount": "serviceaccounts",
    "Job": "jobs",
    "Pod": "pods",
    "Secret": "secrets",
}
_CHAOS_SELECTOR_FIELDS = frozenset({"namespaces", "labelSelectors"})


class JournalCoverageFact(BaseModel):
    """K1: persisted, continuous object-journal coverage (never a point-in-time LIST)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_start: datetime
    coverage_end: datetime
    covered_namespaces: frozenset[str]
    covered_kinds: frozenset[str]
    continuous: bool
    gap_count: int
    source: str
    source_version: str
    evidence_id: str


class ApiServerAuditCoverage(BaseModel):
    """Audit coverage and loss-counter continuity of one serving API-server instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance: str
    coverage_start: datetime
    coverage_end: datetime
    # Same process and series across the window (no reset, restart, scrape gap).
    counter_continuous: bool
    error_count_start: int | None = None
    error_count_end: int | None = None


class ApiAccess(BaseModel):
    """One audited API request; ``ended_at`` None means still open (a live watch)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user: str
    impersonated_user: str | None = None
    verb: str
    kind: str
    namespace: str | None = None
    name: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    evidence_id: str


class ApiAuditEvidence(BaseModel):
    """K6 positive evidence: complete Kubernetes audit coverage for the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    serving_instances: frozenset[str]
    instances: tuple[ApiServerAuditCoverage, ...]
    policy_covers_relevant_requests: bool
    active_watches_at_start_known: bool
    accesses: tuple[ApiAccess, ...] = ()


@dataclass
class KEvaluation:
    """The K channel result plus what it contributes to closure and channel C."""

    state: ChannelState
    gap_reason: str | None
    preconditions: tuple[ChannelPrecondition, ...]
    evidence_ids: tuple[str, ...]
    expansion: set[EntityRef] = field(default_factory=set)
    control_plane_access: tuple[str, ...] = ()


def _pass(name: str) -> ChannelPrecondition:
    return ChannelPrecondition(precondition=name, status=CoveragePreconditionStatus.PASS)


def _fail(name: str, reason: str, evidence: Iterable[str] = ()) -> ChannelPrecondition:
    return ChannelPrecondition(
        precondition=name,
        status=CoveragePreconditionStatus.FAIL,
        reason=reason,
        evidence_ids=tuple(sorted(set(evidence)))[:12],
    )


def _unknown(name: str, reason: str) -> ChannelPrecondition:
    return ChannelPrecondition(
        precondition=name, status=CoveragePreconditionStatus.UNKNOWN, reason=reason
    )


def window_versions(
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    start: datetime | None,
    end: datetime | None,
) -> dict[EntityRef, list[ObjectVersion]]:
    """Every version relevant to the window: the one in effect at its start, then all inside.

    An unknown window keeps every version (a superset can only add PATH evidence).
    """
    relevant: dict[EntityRef, list[ObjectVersion]] = {}
    for ref, versions in history.items():
        kept: list[ObjectVersion] = []
        for version in versions:
            if end is not None and version.observed_at > end:
                break
            if start is not None and version.observed_at <= start:
                kept = [version]
            else:
                kept.append(version)
        if kept:
            relevant[ref] = kept
    return relevant


def window_edges(
    versions: Mapping[EntityRef, Sequence[ObjectVersion]],
    events: Sequence[ClusterEvent],
) -> set[Edge]:
    """The union of edges derived from every relevant version (K4), not only the latest."""
    edges: set[Edge] = set()
    depth = max((len(items) for items in versions.values()), default=0)
    for generation in range(depth):
        snapshot = {ref: items[min(generation, len(items) - 1)] for ref, items in versions.items()}
        edges.update(derive_edges(snapshot, events))
    return edges


def _k4_path(
    closure: set[EntityRef], symptom_side: set[EntityRef], topology: Topology
) -> tuple[str, ...] | None:
    """Visited-set traversal of the finite graph; the bound is the graph size, not a policy."""
    bound = len({e.source for e in topology.edges} | {e.target for e in topology.edges}) + 1
    for ref in sorted(closure, key=lambda item: item.canonical):
        path = topology.causal_path(ref, symptom_side, max_depth=bound)
        if path is not None:
            return tuple(f"{hop.source.canonical}->{hop.target.canonical}" for hop in path) or (
                ref.canonical,
            )
    return None


def _k5_surfaces(
    versions: Mapping[EntityRef, Sequence[ObjectVersion]], closure: set[EntityRef]
) -> list[tuple[str, str]]:
    """Known unsupported reference surfaces present in the window, as (reason, evidence id)."""
    found: list[tuple[str, str]] = []
    for ref, items in versions.items():
        for version in items:
            body = version.body
            spec = child(body, "spec")
            if ref.kind == "NetworkPolicy":
                for direction, peers in (("ingress", "from"), ("egress", "to")):
                    for rule in spec.get(direction) or []:
                        for peer in (rule.get(peers) or []) if isinstance(rule, dict) else []:
                            if isinstance(peer, dict) and "namespaceSelector" in peer:
                                found.append(("K5_NETPOL_NAMESPACE_SELECTOR", version.evidence_id))
            if ref.kind == "Service" and spec.get("type") == "ExternalName":
                found.append(("K5_EXTERNAL_NAME_SERVICE", version.evidence_id))
            selectors = []
            if ref.kind == "Service":
                selectors.append(spec.get("selector"))
            if ref.kind == "NetworkPolicy":
                selectors.append(spec.get("podSelector"))
            if any(isinstance(s, dict) and "matchExpressions" in s for s in selectors):
                found.append(("K5_UNEVALUATED_SELECTOR", version.evidence_id))
            chaos_selector = spec.get("selector")
            if (ref.kind.endswith("Chaos") or ref.kind in {"Schedule", "Workflow"}) and (
                isinstance(chaos_selector, dict) and set(chaos_selector) - _CHAOS_SELECTOR_FIELDS
            ):
                found.append(("K5_UNEVALUATED_SELECTOR", version.evidence_id))
            pod_spec = _pod_spec(body)
            if pod_spec is not None:
                if ref.kind == "Pod" and pod_spec.get("ephemeralContainers"):
                    found.append(("K5_EPHEMERAL_CONTAINERS", version.evidence_id))
                affinity = child(pod_spec, "affinity")
                for key in ("podAffinity", "podAntiAffinity"):
                    terms = child(affinity, key)
                    for mode in (
                        "requiredDuringSchedulingIgnoredDuringExecution",
                        "preferredDuringSchedulingIgnoredDuringExecution",
                    ):
                        for term in terms.get(mode) or []:
                            inner = (
                                term.get("podAffinityTerm", term) if isinstance(term, dict) else {}
                            )
                            if isinstance(inner, dict) and (
                                inner.get("namespaces") or "namespaceSelector" in inner
                            ):
                                found.append(("K5_CROSS_NAMESPACE_AFFINITY", version.evidence_id))
            if ref in closure and ref.kind in KIND_PROFILE:
                if body.get("apiVersion") != KIND_PROFILE[ref.kind][0]:
                    found.append(("K5_API_VERSION_OUTSIDE_PROFILE", version.evidence_id))
    return found


def _service_account(pod_spec: Mapping[str, object], namespace: str) -> str:
    name = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount") or "default"
    return f"system:serviceaccount:{namespace}:{name}"


def _identity_pods(
    versions: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> dict[str, set[EntityRef]]:
    pods: dict[str, set[EntityRef]] = {}
    for ref, items in versions.items():
        if ref.kind != "Pod":
            continue
        for version in items:
            pod_spec = _pod_spec(version.body)
            if pod_spec is not None:
                pods.setdefault(_service_account(pod_spec, ref.namespace), set()).add(ref)
    return pods


def _closure_identities(
    closure: set[EntityRef], versions: Mapping[EntityRef, Sequence[ObjectVersion]]
) -> set[str]:
    identities = {
        f"system:serviceaccount:{ref.namespace}:{ref.name}"
        for ref in closure
        if ref.kind == "ServiceAccount"
    }
    for ref in closure:
        for version in versions.get(ref, ()):
            pod_spec = _pod_spec(version.body)
            if pod_spec is not None:
                identities.add(_service_account(pod_spec, ref.namespace))
    return identities


def _touches(access: ApiAccess, member: EntityRef) -> bool:
    if access.kind != member.kind:
        return False
    member_namespace = None if member.namespace == CLUSTER_SCOPE else member.namespace
    if access.name is None:  # a collection request reaches every object in its scope
        return access.namespace is None or access.namespace == member_namespace
    return access.name == member.name and access.namespace == member_namespace


def _node_readers(
    identity: str,
    access: ApiAccess,
    member: EntityRef,
    versions: Mapping[EntityRef, Sequence[ObjectVersion]],
) -> list[EntityRef]:
    """Exact kubelet attribution: Pods on the node whose spec references the object."""
    node = identity.removeprefix("system:node:")
    if access.name is None:
        return []
    pods: set[EntityRef] = set()
    for ref, items in versions.items():
        if ref.kind != "Pod" or ref.namespace != member.namespace:
            continue
        for version in items:
            pod_spec = _pod_spec(version.body)
            if pod_spec is None or pod_spec.get("nodeName") != node:
                continue
            refs = set(_config_refs(pod_spec))
            refs.add(("ServiceAccount", str(pod_spec.get("serviceAccountName") or "default")))
            for volume in pod_spec.get("volumes") or []:
                claim = child(volume, "persistentVolumeClaim") if isinstance(volume, dict) else {}
                if isinstance(claim.get("claimName"), str):
                    refs.add(("PersistentVolumeClaim", claim["claimName"]))
            if (member.kind, member.name) in refs:
                pods.add(ref)
    return sorted(pods, key=lambda item: item.canonical)


def _rbac_permissions(
    closure: set[EntityRef],
    identities: set[str],
    latest: Mapping[EntityRef, ObjectVersion],
) -> tuple[list[str], bool]:
    """Bindings that let an outside subject read a closure object or a closure identity write.

    Returns the offending binding evidence ids and whether any role was unresolved.
    """
    roles: dict[tuple[str, str, str], list[Any]] = {}
    for ref, version in latest.items():
        if ref.kind in {"Role", "ClusterRole"}:
            rules = version.body.get("rules")
            roles[(ref.namespace, ref.kind, ref.name)] = rules if isinstance(rules, list) else []
    closure_namespaces = {ref.namespace for ref in closure}
    kinds = {ref.kind for ref in closure}
    offending: list[str] = []
    unresolved = False
    for ref, version in latest.items():
        if ref.kind not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        role_ref = child(version.body, "roleRef")
        role_kind = str(role_ref.get("kind") or "")
        role_namespace = ref.namespace if role_kind == "Role" else CLUSTER_SCOPE
        rules = roles.get((role_namespace, role_kind, str(role_ref.get("name") or "")))
        if rules is None:
            unresolved = True
            continue
        subjects = version.body.get("subjects") or []
        names = set()
        for subject in subjects if isinstance(subjects, list) else []:
            if not isinstance(subject, dict):
                continue
            if subject.get("kind") == "ServiceAccount":
                names.add(
                    f"system:serviceaccount:{subject.get('namespace') or ref.namespace}:"
                    f"{subject.get('name')}"
                )
            else:
                names.add(f"{subject.get('kind')}:{subject.get('name')}")
        scope_hits = ref.kind == "ClusterRoleBinding" or ref.namespace in closure_namespaces
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            groups = set(rule.get("apiGroups") or [])
            resources = set(rule.get("resources") or [])
            verbs = set(rule.get("verbs") or [])
            reads = scope_hits and any(
                ({_API_GROUP.get(kind, ""), "*"} & groups)
                and ({_RESOURCE.get(kind, ""), "*"} & resources)
                for kind in kinds
            )
            if reads and verbs & _READ_VERBS and names - identities:
                offending.append(version.evidence_id)
                break
            outside = ref.kind == "ClusterRoleBinding" or ref.namespace not in closure_namespaces
            if outside and verbs & _WRITE_VERBS and names & identities:
                offending.append(version.evidence_id)
                break
    return offending, unresolved


def _k6(
    closure: set[EntityRef],
    versions: Mapping[EntityRef, Sequence[ObjectVersion]],
    latest: Mapping[EntityRef, ObjectVersion],
    audit: ApiAuditEvidence | None,
    start: datetime | None,
    end: datetime | None,
) -> tuple[ChannelPrecondition, set[EntityRef], tuple[str, ...]]:
    identities = _closure_identities(closure, versions)
    if audit is None:
        offending, unresolved = _rbac_permissions(closure, identities, latest)
        if offending:
            return _fail("K6", "K6_PERMISSION_WITHOUT_AUDIT_EVIDENCE", offending), set(), ()
        reason = "K6_RBAC_ROLE_UNRESOLVED" if unresolved else "K6_API_AUDIT_EVIDENCE_ABSENT"
        return _unknown("K6", reason), set(), ()
    if start is None or end is None:
        return _unknown("K6", "K6_WINDOW_UNKNOWN"), set(), ()
    failures: list[tuple[str, str]] = []
    unknowns: list[str] = []
    covered = {item.instance: item for item in audit.instances}
    for instance in sorted(audit.serving_instances):
        coverage = covered.get(instance)
        if coverage is None or coverage.coverage_start > start or coverage.coverage_end < end:
            unknowns.append("K6_AUDIT_COVERAGE_INCOMPLETE")
            continue
        counts = (coverage.error_count_start, coverage.error_count_end)
        if not coverage.counter_continuous or None in counts:
            unknowns.append("K6_AUDIT_LOSS_UNPROVEN")
        elif counts[1] != counts[0]:
            failures.append(("K6_AUDIT_EVENTS_LOST", instance))
    if not audit.policy_covers_relevant_requests:
        unknowns.append("K6_AUDIT_POLICY_UNPROVEN")
    if not audit.active_watches_at_start_known:
        failures.append(("K6_PRE_WINDOW_WATCHES_UNKNOWN", ""))
    identity_pods = _identity_pods(versions)
    expansion: set[EntityRef] = set()
    control_plane: list[str] = []
    attributions: list[str] = []

    def attribute(identity: str, access: ApiAccess, members: list[EntityRef]) -> str | None:
        """Attribute one identity's read of closure objects; None when it cannot be resolved."""
        if identity in identities:
            return "closure"
        if identity in CONTROL_PLANE_USERS | CONTROL_PLANE_SERVICE_ACCOUNTS:
            return "control-plane"
        if identity.startswith("system:node:"):
            pods = [
                pod
                for member in members
                for pod in _node_readers(identity, access, member, versions)
            ]
            if pods and all(
                _node_readers(identity, access, member, versions) for member in members
            ):
                return "kubelet:" + ",".join(sorted({pod.canonical for pod in pods}))
            return None
        readers = identity_pods.get(identity)
        if identity.startswith("system:serviceaccount:") and readers:
            return "workload:" + ",".join(sorted(pod.canonical for pod in readers))
        return None

    for access in audit.accesses:
        if access.started_at > end or (access.ended_at is not None and access.ended_at < start):
            continue
        members = [member for member in closure if _touches(access, member)]
        verb = access.verb.lower()
        # Provenance keeps both actors (§4.5.3); the effective one is the impersonated identity.
        authenticated, effective = access.user, access.impersonated_user or access.user
        if verb in _READ_VERBS and members:
            results = {
                actor: attribute(actor, access, members) for actor in (authenticated, effective)
            }
            attributions.append(
                f"{access.evidence_id}: authenticated={authenticated}->{results[authenticated]}; "
                f"effective={effective}->{results[effective]}"
            )
            if None in results.values():
                reason = (
                    "K6_UNATTRIBUTED_NODE_ACCESS"
                    if any(a.startswith("system:node:") for a, r in results.items() if r is None)
                    else "K6_UNBOUND_ACCESS"
                )
                failures.append((reason, access.evidence_id))
                continue
            if "control-plane" in results.values():
                control_plane.append(access.evidence_id)
            # The closure expands from the attributable effective access only.
            if (results[effective] or "").startswith("workload:"):
                expansion |= identity_pods[effective]
        elif verb in _WRITE_VERBS and {authenticated, effective} & identities and not members:
            attributions.append(
                f"{access.evidence_id}: authenticated={authenticated}; effective={effective}; write"
            )
            if access.name is None:
                failures.append(("K6_UNBOUND_WRITE_TARGET", access.evidence_id))
            else:
                expansion.add(
                    EntityRef(
                        kind=access.kind,
                        name=access.name,
                        namespace=access.namespace or CLUSTER_SCOPE,
                    )
                )
    if failures:
        k6 = _fail("K6", failures[0][0], [e for _, e in failures if e])
    elif unknowns:
        k6 = _unknown("K6", unknowns[0])
    else:
        k6 = _pass("K6")
    return (
        k6.model_copy(update={"attributions": tuple(attributions)}),
        expansion,
        tuple(control_plane),
    )


def evaluate_k(
    closure: set[EntityRef],
    *,
    history: Mapping[EntityRef, Sequence[ObjectVersion]],
    events: Sequence[ClusterEvent],
    topology: Topology,
    symptom_entities: set[EntityRef],
    window_start: datetime | None,
    window_end: datetime | None,
    journal_coverage: JournalCoverageFact | None,
    api_audit: ApiAuditEvidence | None,
) -> KEvaluation:
    """Evaluate K1–K6 for one closure and apply the §4.5.5 precedence."""
    latest = {ref: items[-1] for ref, items in history.items() if items}
    versions = window_versions(history, window_start, window_end)
    closure_namespaces = {ref.namespace for ref in closure if ref.namespace != CLUSTER_SCOPE}
    symptom_namespaces = {
        ref.namespace for ref in symptom_entities if ref.namespace != CLUSTER_SCOPE
    }

    # K1 — continuous journal coverage, positively proven.
    if journal_coverage is None:
        k1 = _unknown("K1", "K1_JOURNAL_COVERAGE_FACT_ABSENT")
    elif window_start is None or window_end is None:
        k1 = _unknown("K1", "K1_WINDOW_UNKNOWN")
    elif not journal_coverage.continuous or journal_coverage.gap_count:
        k1 = _fail("K1", "K1_JOURNAL_NOT_CONTINUOUS", [journal_coverage.evidence_id])
    elif (
        journal_coverage.coverage_start > window_start
        or journal_coverage.coverage_end < window_end
        or not (closure_namespaces | symptom_namespaces | {CLUSTER_SCOPE})
        <= journal_coverage.covered_namespaces
        or not (REQUIRED_JOURNAL_KINDS | {ref.kind for ref in closure})
        <= journal_coverage.covered_kinds
    ):
        k1 = _fail("K1", "K1_JOURNAL_COVERAGE_INSUFFICIENT", [journal_coverage.evidence_id])
    else:
        k1 = _pass("K1")

    # K2 — every closure kind COMPLETE in the frozen profile.
    incomplete = sorted(
        {ref.kind for ref in closure if not KIND_PROFILE.get(ref.kind, ("", False))[1]}
    )
    k2 = (
        _fail("K2", "K2_KIND_PROFILE_INCOMPLETE:" + ",".join(incomplete))
        if incomplete
        else _pass("K2")
    )

    # K3 — namespace separation. Only alert entities are known here, not the full
    # OD-A4 symptom side, so separation can be refuted but not proven.
    shared = closure_namespaces & symptom_namespaces
    k3 = (
        _fail("K3", "SAME_NAMESPACE_REFERENCE_SURFACE_INCOMPLETE")
        if shared
        else _unknown("K3", "K3_SYMPTOM_SIDE_INCOMPLETE")
    )

    # K4 — complete traversal over every relevant version in the window.
    union = Topology(set(topology.edges) | window_edges(versions, events), latest)
    path = _k4_path(closure, symptom_entities, union)
    if path is not None:
        k4 = _fail("K4", "K4_MODELED_PATH")
    elif window_start is None or window_end is None:
        k4 = _unknown("K4", "K4_WINDOW_UNKNOWN")
    else:
        k4 = _pass("K4")

    # K5 — no known unsupported reference surface.
    surfaces = _k5_surfaces(versions, closure)
    if surfaces:
        k5 = _fail("K5", surfaces[0][0], [evidence for _, evidence in surfaces])
    elif incomplete:
        k5 = _unknown("K5", "K5_SCHEMA_PROFILE_INCOMPLETE")
    else:
        k5 = _pass("K5")

    # K6 — API surface positively covered by audit evidence.
    k6, expansion, control_plane = _k6(
        closure, versions, latest, api_audit, window_start, window_end
    )

    preconditions = (k1, k2, k3, k4, k5, k6)
    failed = [p for p in preconditions if p.status is CoveragePreconditionStatus.FAIL]
    missing = [p for p in preconditions if p.status is CoveragePreconditionStatus.UNKNOWN]
    if path is not None:
        state, reason = ChannelState.PATH, None
    elif failed:
        state, reason = ChannelState.UNCOVERED, failed[0].reason
    elif missing:
        state, reason = ChannelState.UNKNOWN, missing[0].reason
    else:
        state, reason = ChannelState.NO_PATH_COVERED, None
    return KEvaluation(
        state=state,
        gap_reason=reason,
        preconditions=preconditions,
        evidence_ids=(),
        expansion=expansion - closure,
        control_plane_access=control_plane,
    )


__all__ = [
    "CONTROL_PLANE_PROFILE",
    "KIND_PROFILE",
    "RULE_ID",
    "RULE_VERSION",
    "ApiAccess",
    "ApiAuditEvidence",
    "ApiServerAuditCoverage",
    "JournalCoverageFact",
    "KEvaluation",
    "evaluate_k",
    "window_edges",
    "window_versions",
]
