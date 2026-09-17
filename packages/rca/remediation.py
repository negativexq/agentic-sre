"""Remediation proposals. The product never executes them."""

from __future__ import annotations

from packages.rca.model import Candidate, EntityRef, FindingKind, Remediation
from packages.rca.topology import Topology, pod_workload_name


def _kubectl_ref(entity: EntityRef) -> str:
    namespace = "" if entity.namespace == "_cluster" else f" -n {entity.namespace}"
    return f"{entity.kind.lower()}/{entity.name}{namespace}"


def propose(candidate: Candidate, topology: Topology) -> tuple[Remediation, ...]:
    """Suggest the smallest reversible action for the strongest finding."""
    finding = candidate.findings[0]
    entity = candidate.entity
    ref = _kubectl_ref(entity)
    if finding.kind is FindingKind.CONFIG_CHANGE:
        keys = ", ".join(finding.details.get("changed_keys", [])[:3]) or "changed keys"
        users = sorted(
            {
                (topology.workload_of(user) or user).name
                for user in topology.incoming(entity, "uses_config")
            }
        )
        restart = f"; then restart {', '.join(users)} if they do not reload config" if users else ""
        return (
            Remediation(
                action=f"Revert {entity.kind} {entity.name} ({keys}) to its previous version{restart}",
                command=f"kubectl get {ref} -o yaml  # compare with the previous version, then kubectl apply",
                risk="low: restores the last known configuration",
            ),
        )
    if finding.kind in {FindingKind.FAULT_INJECTION, FindingKind.FAULT_SCHEDULE}:
        parents = [
            edge.source
            for edge in topology.edges
            if edge.relation == "spawns" and edge.target == entity
        ]
        schedule = parents[0] if parents else entity
        actions = [
            Remediation(
                action=f"Pause the fault experiment {schedule.kind} {schedule.name}",
                command=(
                    f"kubectl annotate {_kubectl_ref(schedule)} "
                    "experiment.chaos-mesh.org/pause=true --overwrite"
                ),
                risk="low: stops further fault injection",
            )
        ]
        if schedule != entity:
            actions.append(
                Remediation(
                    action=f"Delete the running experiment {entity.kind} {entity.name}",
                    command=f"kubectl delete {ref}",
                    risk="low: removes the active fault",
                )
            )
        return tuple(actions)
    if finding.kind in {FindingKind.IMAGE_CHANGE, FindingKind.SPEC_CHANGE} and entity.kind in {
        "Deployment",
        "StatefulSet",
        "DaemonSet",
    }:
        return (
            Remediation(
                action=f"Roll back {entity.kind} {entity.name} to the previous revision",
                command=f"kubectl rollout undo {ref}",
                risk="medium: reverts every change in the last rollout",
            ),
        )
    if finding.kind is FindingKind.SCALE_CHANGE:
        before = finding.details.get("before")
        return (
            Remediation(
                action=f"Restore {entity.name} replicas to {before}",
                command=f"kubectl scale {ref} --replicas={before}",
                risk="low",
            ),
        )
    if finding.kind in {FindingKind.QUOTA_EXCEEDED, FindingKind.QUOTA_EXHAUSTED}:
        exhausted = ", ".join(finding.details.get("exhausted", [])) or "the rejected resources"
        return (
            Remediation(
                action=f"Raise {entity.kind} {entity.name} limits for {exhausted}, or lower workload requests",
                command=f"kubectl describe {ref}  # compare used with hard, then kubectl edit {ref}",
                risk="medium: more capacity for the namespace",
            ),
        )
    if finding.kind is FindingKind.CONTAINER_FAILURE and entity.kind == "Pod":
        workload = topology.workload_of(entity)
        target = (
            _kubectl_ref(workload) if workload else f"deployment/{pod_workload_name(entity.name)}"
        )
        reason = finding.details.get("reason", "")
        if reason == "OOMKilled":
            return (
                Remediation(
                    action=f"Raise the memory limit of {target} or find the memory growth",
                    command=f"kubectl set resources {target} --limits=memory=<higher>",
                    risk="medium: uses more node memory",
                ),
            )
        if reason in {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}:
            return (
                Remediation(
                    action=f"Fix the image reference of {target} or roll it back",
                    command=f"kubectl rollout undo {target}",
                    risk="medium: reverts the last rollout",
                ),
            )
        return (
            Remediation(
                action=f"Read the logs of the failing container, then fix or roll back {target}",
                command=f"kubectl logs {ref} --previous && kubectl rollout undo {target}",
                risk="medium: reverts the last rollout",
            ),
        )
    if finding.kind is FindingKind.RESOURCE_PRESSURE and entity.kind == "Pod":
        workload = topology.workload_of(entity)
        target = (
            _kubectl_ref(workload) if workload else f"deployment/{pod_workload_name(entity.name)}"
        )
        resource = "memory" if "memory" in finding.details.get("resources", []) else "cpu"
        return (
            Remediation(
                action=f"Check what drives {resource} use in {target}, then raise its {resource} limit",
                command=f"kubectl top pod {entity.name} -n {entity.namespace} --containers",
                risk="medium: a higher limit uses more node capacity",
            ),
        )
    if finding.kind in {FindingKind.POLICY_CREATED, FindingKind.NETWORK_RESTRICTION}:
        return (
            Remediation(
                action=f"Review and relax {entity.kind} {entity.name}",
                command=f"kubectl describe {ref}",
                risk="medium: loosening a policy can widen access",
            ),
        )
    if entity.kind == "Pod":
        workload = topology.workload_of(entity)
        target = (
            _kubectl_ref(workload) if workload else f"deployment/{pod_workload_name(entity.name)}"
        )
        return (
            Remediation(
                action=f"Inspect {entity.name}, then restart {target} if it stays unhealthy",
                command=f"kubectl describe {ref} && kubectl rollout restart {target}",
                risk="medium: restart drops in-flight requests",
            ),
        )
    return (
        Remediation(
            action=f"Inspect {entity.kind} {entity.name}",
            command=f"kubectl describe {ref}",
            risk="none",
            requires_approval=False,
        ),
    )


__all__ = ["propose"]
