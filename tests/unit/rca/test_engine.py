"""End-to-end diagnosis on in-memory incidents."""

from __future__ import annotations

from rca_builders import alert, at, config_change_source, event, ref, shop_objects, version

from packages.rca.engine import Case, Choice, build_case, diagnose
from packages.rca.model import Confidence, FindingKind, ResourcePressure
from packages.rca.source import InMemorySource


def test_config_change_is_diagnosed_verified_with_revert_proposal() -> None:
    diagnosis = diagnose(config_change_source())
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")
    assert diagnosis.confidence is Confidence.VERIFIED
    assert diagnosis.evidence[0].kind is FindingKind.CONFIG_CHANGE
    assert diagnosis.remediation[0].action.startswith("Revert ConfigMap checkout-flags")
    assert "checkout" in diagnosis.remediation[0].action
    assert all(item.requires_approval for item in diagnosis.remediation)
    assert ref("infra/ConfigMap/recorder") in {c.entity for c in diagnosis.alternatives}
    assert diagnosis.mode == "deterministic" and diagnosis.model_calls == 0


def test_unlinked_change_ranks_below_linked_change() -> None:
    diagnosis = diagnose(config_change_source())
    recorder = next(c for c in diagnosis.alternatives if c.entity.name == "recorder")
    assert "no link to the alerting components" in recorder.reasons


def test_chaos_experiment_on_alerting_pod_is_verified_and_collapsed() -> None:
    target = "Successfully apply chaos for shop/checkout-5d8f7c9b4-abcde"
    source = InMemorySource(
        name="chaos",
        alert_items=[alert("RequestLatency", "checkout", 6)],
        versions=shop_objects(0),
        event_items=[
            event("chaos/Schedule/checkout-delay", "Spawned", 1),
            event("chaos/NetworkChaos/checkout-delay-aaaaa", "Applied", 1, message=target),
            event("chaos/NetworkChaos/checkout-delay-bbbbb", "Applied", 5, message=target),
        ],
    )
    diagnosis = diagnose(source)
    assert diagnosis.root_cause == ref("chaos/NetworkChaos/checkout-delay-bbbbb")
    assert diagnosis.confidence is Confidence.VERIFIED
    names = [c.entity.name for c in diagnosis.alternatives]
    assert "checkout-delay-aaaaa" not in names
    assert "checkout-delay" in names
    assert "pause=true" in diagnosis.remediation[0].command


def test_restrictive_network_policy_is_verified() -> None:
    policy = version(
        "shop/NetworkPolicy/deny-checkout",
        0,
        {"spec": {"podSelector": {"matchLabels": {"app": "checkout"}}, "policyTypes": ["Ingress"]}},
    )
    source = InMemorySource(
        name="policy",
        alert_items=[alert("RequestErrorRate", "checkout", 3)],
        versions=[*shop_objects(0), policy],
    )
    diagnosis = diagnose(source)
    assert diagnosis.root_cause == ref("shop/NetworkPolicy/deny-checkout")
    assert diagnosis.confidence is Confidence.VERIFIED


def test_failure_event_only_is_never_verified() -> None:
    source = InMemorySource(
        name="crash",
        alert_items=[alert("RequestErrorRate", "checkout", 3)],
        versions=shop_objects(0),
        event_items=[
            event("shop/Pod/checkout-5d8f7c9b4-abcde", "BackOff", 2, type_="Warning", count=9)
        ],
    )
    diagnosis = diagnose(source)
    assert diagnosis.root_cause == ref("shop/Pod/checkout-5d8f7c9b4-abcde")
    assert diagnosis.confidence is not Confidence.VERIFIED
    assert "rollout restart" in diagnosis.remediation[0].command


def test_no_signal_returns_explicit_empty_diagnosis() -> None:
    source = InMemorySource(
        name="quiet",
        alert_items=[alert("RequestErrorRate", "checkout", 3)],
        versions=shop_objects(0),
    )
    diagnosis = diagnose(source)
    assert diagnosis.root_cause is None
    assert diagnosis.confidence is Confidence.UNVERIFIED


def test_investigator_cannot_replace_a_verified_answer_with_an_unverified_one() -> None:
    class Picker:
        name = "scripted"

        def investigate(self, case: Case) -> Choice | None:
            recorder = next(c for c in case.candidates if c.entity.name == "recorder")
            return Choice(entity=recorder.entity, rationale="looked suspicious", model_calls=2)

    diagnosis = diagnose(config_change_source(), investigator=Picker())
    assert diagnosis.root_cause == ref("shop/ConfigMap/checkout-flags")
    assert diagnosis.confidence is Confidence.VERIFIED
    assert diagnosis.mode == "scripted" and diagnosis.model_calls == 2
    assert any(step.actor == "scripted" for step in diagnosis.steps)
    kept = [step for step in diagnosis.steps if step.action == "kept"]
    assert kept and "only UNVERIFIED" in kept[0].detail


def test_investigator_may_reorder_candidates_of_equal_confidence() -> None:
    source = config_change_source()
    source.versions += [
        version("shop/ConfigMap/routing", 0, {"data": {"route": "checkout-v1"}}),
        version("shop/ConfigMap/routing", 9, {"data": {"route": "checkout-v2"}}, 1),
    ]

    class Picker:
        name = "scripted"

        def investigate(self, case: Case) -> Choice | None:
            routing = next(c for c in case.candidates if c.entity.name == "routing")
            return Choice(entity=routing.entity, rationale="also verified")

    diagnosis = diagnose(source, investigator=Picker())
    assert diagnosis.root_cause == ref("shop/ConfigMap/routing")
    assert diagnosis.confidence is Confidence.VERIFIED


def test_dependency_outage_is_blamed_over_the_callers_own_warnings() -> None:
    from rca_builders import microservice

    from packages.rca.model import LogRecord

    source = InMemorySource(
        name="dependency",
        alert_items=[alert("RequestErrorRate", "cart", 5)],
        versions=[
            *microservice("cart", 0, {"STORE_ADDR": "store:6379"}),
            *microservice("store", 0),
        ],
        event_items=[
            event("shop/Pod/cart-5d8f7c9b4-abcde", "FailedKillPod", 4, type_="Warning", count=40)
        ],
        error_items=[
            LogRecord(
                service="cart",
                at=at(4),
                severity="ERROR",
                message="could not connect to store",
                evidence_id="log:1",
            )
        ],
    )
    diagnosis = diagnose(source)
    assert diagnosis.root_cause == ref("shop/Pod/store-5d8f7c9b4-abcde")
    assert diagnosis.confidence is Confidence.LIKELY


def test_builtin_demo_finds_the_bad_rollout() -> None:
    from packages.rca.demo import demo_source

    diagnosis = diagnose(demo_source())
    assert diagnosis.root_cause == ref("shop/Deployment/payment")
    assert diagnosis.confidence is Confidence.VERIFIED
    assert "FAULT_DELAY_MS" in diagnosis.summary
    assert "rollout undo" in diagnosis.remediation[0].command


def _quota(used: str) -> dict[str, object]:
    return {"status": {"hard": {"limits.memory": "1Gi"}, "used": {"limits.memory": used}}}


def test_quota_that_rejects_pods_is_verified_with_a_capacity_proposal() -> None:
    source = InMemorySource(
        name="quota",
        alert_items=[alert("RequestErrorRate", "checkout", 5)],
        versions=[*shop_objects(0), version("shop/ResourceQuota/mem", 0, _quota("1024Mi"))],
        event_items=[
            event(
                "shop/ReplicaSet/checkout-5d8f7c9b4",
                "FailedCreate",
                3,
                type_="Warning",
                count=4,
                message="pods is forbidden: exceeded quota: mem, requested: limits.memory=256Mi",
            )
        ],
    )
    diagnosis = diagnose(source)
    assert diagnosis.root_cause == ref("shop/ResourceQuota/mem")
    assert diagnosis.confidence is Confidence.VERIFIED
    assert "limits.memory" in diagnosis.remediation[0].action


def test_quota_with_headroom_is_not_a_candidate() -> None:
    source = InMemorySource(
        name="quota-ok",
        alert_items=[alert("RequestErrorRate", "checkout", 5)],
        versions=[*shop_objects(0), version("shop/ResourceQuota/mem", 0, _quota("512Mi"))],
    )
    assert all(c.entity.kind != "ResourceQuota" for c in build_case(source).candidates)


def test_partial_network_policy_is_a_candidate_but_not_verified() -> None:
    policy = version(
        "shop/NetworkPolicy/only-8080",
        0,
        {
            "spec": {
                "podSelector": {"matchLabels": {"app": "checkout"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"port": 8080}]}],
            }
        },
    )
    source = InMemorySource(
        name="partial-policy",
        alert_items=[alert("RequestErrorRate", "checkout", 3)],
        versions=[*shop_objects(0), policy],
    )
    diagnosis = diagnose(source)
    finding = next(
        c for c in build_case(source).candidates if c.entity == ref("shop/NetworkPolicy/only-8080")
    ).findings[0]
    assert finding.kind is FindingKind.NETWORK_RESTRICTION
    assert "ingress allows TCP/8080 from any peer" in finding.summary
    assert diagnosis.confidence is not Confidence.VERIFIED


def test_oom_killed_container_proposes_more_memory() -> None:
    versions = shop_objects(0)
    crashed = versions[2].model_copy(deep=True)
    crashed.body["status"] = {
        "containerStatuses": [
            {
                "name": "checkout",
                "restartCount": 5,
                "state": {"running": {}},
                "lastState": {
                    "terminated": {
                        "reason": "OOMKilled",
                        "exitCode": 137,
                        "finishedAt": at(2).isoformat(),
                    }
                },
            }
        ]
    }
    source = InMemorySource(
        name="oom",
        alert_items=[alert("RequestErrorRate", "checkout", 3)],
        versions=[*versions[:2], crashed, versions[3]],
    )
    diagnosis = diagnose(source)
    assert diagnosis.root_cause == ref("shop/Pod/checkout-5d8f7c9b4-abcde")
    assert diagnosis.evidence[0].kind is FindingKind.CONTAINER_FAILURE
    assert "OOMKilled" in diagnosis.evidence[0].summary
    assert "memory" in diagnosis.remediation[0].command
    assert diagnosis.confidence is not Confidence.VERIFIED


def test_new_memory_pressure_on_the_alerting_pod_is_reported() -> None:
    pod = ref("shop/Pod/checkout-5d8f7c9b4-abcde")
    source = InMemorySource(
        name="pressure",
        alert_items=[alert("RequestErrorRate", "checkout", 10)],
        versions=shop_objects(0),
        pressure_items=[
            ResourcePressure(
                pod=pod,
                container="checkout",
                resource="memory",
                baseline=0.4,
                peak=0.98,
                at=at(9),
                evidence_id="metrics:1",
            )
        ],
    )
    diagnosis = diagnose(source)
    assert diagnosis.root_cause == pod
    assert diagnosis.evidence[0].kind is FindingKind.RESOURCE_PRESSURE
    assert "memory limit" in diagnosis.remediation[0].action
