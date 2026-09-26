"""M19-6.11: the agentic-sre.product-run.v2 artifact — observed facts, unevaluated claims, once."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from test_product_runner import READINESS, ROOT, Commands, _control, _Process, _ready_evidence

from packages.evals.product.actions import (
    DeletePodOf,
    PatchService,
    SetReadiness,
    SetResources,
)
from packages.evals.product.artifact import (
    PROTOCOL_DOCUMENT,
    SCHEMA,
    ArtifactError,
    Attempt,
    LocalGit,
    ProductRunArtifact,
    ProofStatus,
    ProviderTape,
    RequirementEntry,
    build_artifact,
    describe_action,
    format_offset,
    load_artifact,
    provenance,
    run_id,
    write_artifact,
)
from packages.evals.product.live import LiveArtifactReader, LiveBackend, LiveEvidenceReader
from packages.evals.product.revisions import R1, AlertActivation, REarly
from packages.evals.product.runner import (
    ProductRunner,
    RecordingBackend,
    RunResult,
    RunStatus,
    Stage,
    TimelineRecord,
)
from packages.evals.product.spec import Expectation, Phase, ProductScenario, ProofId
from packages.storage.database import create_session_factory
from packages.storage.models import Base, EvidenceRequirementRow, InvestigationReadRow

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
ONSET = T0 + timedelta(minutes=1)
HEAD = "a" * 40
PROTOCOL = "b" * 40
RUN = "20260926T120000123456Z"
LABELS = {"severity": "page", "alertname": "PaymentServiceErrorsHigh", "service": "payment-service"}


def _summary(number: int, trigger: str, **overrides: Any) -> dict[str, Any]:
    summary = {
        "diagnosis_id": 10 + number,
        "revision_number": number,
        "previous_diagnosis_id": 9 + number if number > 1 else None,
        "trigger": trigger,
        "run_id": f"run-{number}",
        "window_end": (ONSET + timedelta(minutes=8 * number)).isoformat(),
        "manifest_digest": f"m{number}",
        "tape_digest": f"t{number}",
        "epistemic_digest": f"e{number}",
        "engine_version": "engine-1",
        "config_digest": "cfg-1",
        "resolution": "RESOLVED" if number == 3 else "AMBIGUOUS",
    }
    summary.update(overrides)
    return summary


SUMMARIES = [_summary(1, "INITIAL"), _summary(2, "MANUAL"), _summary(3, "EVIDENCE_DEADLINE")]
TAPE = ProviderTape(reads=7, capture_reads=2, engine_reads=3, investigation_reads=2, errors=0)
REQUIREMENT = RequirementEntry(
    requirement_key="k" * 64, rule_id="m16.resource-pressure", not_before=ONSET
)


def _scenario(
    *proofs: ProofId, tier: Any = "DEV", phases: tuple[Phase, ...] = ()
) -> ProductScenario:
    return ProductScenario(
        scenario_id="PR-02", phases=phases, expectation=Expectation(proofs=proofs), tier=tier
    )


@dataclass(frozen=True)
class Verified:
    offset: timedelta
    action: Any
    started_at: datetime


def _build(**overrides: Any) -> ProductRunArtifact:
    facts: dict[str, Any] = {
        "scenario": _scenario(ProofId.T1, ProofId.T2, ProofId.N0),
        "commit": HEAD,
        "protocol_commit": PROTOCOL,
        "incident_id": "i1",
        "onset": ONSET,
        "activations": [
            AlertActivation(LABELS, T0 + timedelta(seconds=90), T0 + timedelta(seconds=75))
        ],
        "timeline": [
            Verified(
                timedelta(minutes=-3),
                SetResources("order-service", {"cpu": "100m"}),
                T0 - timedelta(minutes=3),
            ),
            Verified(timedelta(0), DeletePodOf("payment-service"), T0),
        ],
        "summaries": SUMMARIES,
        "requirements": {11: [REQUIREMENT]},
        "provider_tape": TAPE,
    }
    facts.update(overrides)
    return build_artifact(**facts)


# --- the document ---------------------------------------------------------------------


def test_observed_facts_are_recorded_and_derived_claims_are_not_evaluated() -> None:
    document = _build().document()
    assert document["schema"] == SCHEMA
    assert document["scenario"] == {"id": "PR-02", "tier": "DEV", "protocol_commit": PROTOCOL}
    assert document["code"] == {
        "commit": HEAD,
        "engine_version": "engine-1",
        "config_digest": "cfg-1",
    }
    assert document["incident"]["id"] == "i1"
    assert document["timeline"] == [
        {
            "offset": "-3m",
            "action": "SetResources:order-service",
            "at": "2026-09-26T11:57:00Z",
            "verify": "PASS",
        },
        {
            "offset": "+0m",
            "action": "DeletePodOf:payment-service",
            "at": "2026-09-26T12:00:00Z",
            "verify": "PASS",
        },
    ]
    assert [(item["number"], item["trigger"]) for item in document["revisions"]] == [
        (1, "INITIAL"),
        (2, "MANUAL"),
        (3, "EVIDENCE_DEADLINE"),
    ]
    first = document["revisions"][0]
    assert first["requirements"] == [
        {
            "requirement_key": "k" * 64,
            "rule_id": "m16.resource-pressure",
            "not_before": "2026-09-26T12:01:00Z",
        }
    ]
    assert document["revisions"][1]["requirements"] == []
    assert document["provider_tape"] == TAPE.model_dump()
    # Derived claims: not evaluated, never PASS/FAIL or 0 from 6.11.
    assert document["proof"] == {"T1": "NOT_EVALUATED", "T2": "NOT_EVALUATED"}
    assert document["negatives"] == {"N0": "NOT_EVALUATED"}
    assert document["transition"] is None
    assert document["safety"] == {
        "false_resolved": None,
        "wrong_actor": None,
        "uid_misbinding": None,
        "missing_to_contradiction": None,
        "fabricated": None,
        "replay_divergence": None,
        "unresolved_tape_evidence_id": None,
        "synthetic_cluster_evidence": None,
    }
    assert document["attempts"] == [{"n": 1, "result": "PASS", "reason": ""}]


def test_proof_ids_come_only_from_the_expectation() -> None:
    document = _build(scenario=_scenario()).document()
    assert document["proof"] == {} and document["negatives"] == {}
    document = _build(scenario=_scenario(ProofId.N2)).document()
    assert document["proof"] == {} and document["negatives"] == {"N2": "NOT_EVALUATED"}


def test_a_smoke_scenario_has_no_tier() -> None:
    assert _build(scenario=_scenario(tier=None)).document()["scenario"]["tier"] is None
    with pytest.raises(ValueError, match="tier"):
        _scenario(tier="SMOKE")


def test_alert_activations_are_canonical_provenance() -> None:
    (activation,) = _build().document()["incident"]["alert_activations"]
    assert list(activation["labels"]) == sorted(LABELS)
    assert activation["active_at"] == "2026-09-26T12:01:15Z"
    assert activation["starts_at"] == "2026-09-26T12:01:30Z"


@pytest.mark.parametrize(
    ("offset", "text"),
    [
        (timedelta(minutes=-3), "-3m"),
        (timedelta(0), "+0m"),
        (timedelta(minutes=4, seconds=30), "+4m30s"),
        (timedelta(minutes=-25), "-25m"),
        (timedelta(minutes=90), "+90m"),
    ],
)
def test_offsets_are_the_requested_scenario_offsets(offset: timedelta, text: str) -> None:
    assert format_offset(offset) == text


def test_sub_second_offsets_are_refused() -> None:
    with pytest.raises(ArtifactError, match="whole number of seconds"):
        format_offset(timedelta(milliseconds=1500))


def test_actions_have_stable_descriptors_not_repr() -> None:
    actions = [
        SetReadiness("payment", True, timedelta(seconds=30)),
        DeletePodOf("payment"),
        SetResources("order", {"cpu": "100m"}),
        PatchService("order", {"app": "x"}),
    ]
    assert [describe_action(item) for item in actions] == [
        "SetReadiness:payment",
        "DeletePodOf:payment",
        "SetResources:order",
        "PatchService:order",
    ]


# --- incoherent facts are ERROR -------------------------------------------------------


@pytest.mark.parametrize(
    "summaries",
    [
        pytest.param(
            [*SUMMARIES[:2], _summary(3, "EVIDENCE_DEADLINE", engine_version="engine-2")],
            id="engine",
        ),
        pytest.param([_summary(1, "INITIAL", config_digest="cfg-0"), *SUMMARIES[1:]], id="config"),
        pytest.param([_summary(1, "INITIAL", engine_version=None), *SUMMARIES[1:]], id="missing"),
    ],
)
def test_revisions_of_one_run_share_engine_and_config(summaries: list[dict[str, Any]]) -> None:
    with pytest.raises(ArtifactError, match="engine version"):
        _build(summaries=summaries)


@pytest.mark.parametrize(
    "name", ["window_end", "manifest_digest", "tape_digest", "epistemic_digest", "resolution"]
)
def test_a_revision_missing_a_fact_is_an_error(name: str) -> None:
    with pytest.raises(ArtifactError, match=name):
        _build(summaries=[_summary(1, "INITIAL", **{name: None}), *SUMMARIES[1:]])


@pytest.mark.parametrize(
    "summaries",
    [
        pytest.param(SUMMARIES[:2], id="two-revisions"),
        pytest.param([*SUMMARIES, _summary(4, "MANUAL")], id="four-revisions"),
        pytest.param(
            [SUMMARIES[0], _summary(2, "EVIDENCE_DEADLINE"), _summary(3, "MANUAL")], id="order"
        ),
    ],
)
def test_the_revision_chain_must_be_the_schedule(summaries: list[dict[str, Any]]) -> None:
    with pytest.raises(ArtifactError):
        _build(summaries=summaries)


def test_caller_supplied_attempts_must_end_in_pass() -> None:
    history = [Attempt(n=1, result="ERROR", reason="infra"), Attempt(n=2, result="PASS", reason="")]
    assert [item["n"] for item in _build(attempts=history).document()["attempts"]] == [1, 2]
    with pytest.raises(ArtifactError, match="passing attempt"):
        _build(attempts=[Attempt(n=1, result="ERROR", reason="infra")])


def test_tape_counts_must_add_up() -> None:
    with pytest.raises(ValidationError):
        ProviderTape(reads=3, capture_reads=1, engine_reads=1, investigation_reads=0, errors=0)
    with pytest.raises(ValidationError):
        ProviderTape(reads=1, capture_reads=1, engine_reads=0, investigation_reads=0, errors=2)


# --- strict schema --------------------------------------------------------------------


def _mutated(change: Any) -> str:
    document = _build().document()
    change(document)
    return json.dumps(document)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda d: d.update(extra=1), id="unknown-top-level"),
        pytest.param(
            lambda d: d["safety"].pop("unresolved_tape_evidence_id") and None,
            id="unresolved-tape-counter-required",
        ),
        pytest.param(
            lambda d: d["safety"].update(unresolved_tape_evidence_id=-1),
            id="unresolved-tape-counter-non-negative",
        ),
        pytest.param(lambda d: d.pop("transition"), id="transition-required"),
        pytest.param(lambda d: d.pop("safety"), id="safety-required"),
        pytest.param(lambda d: d.pop("proof"), id="proof-required"),
        pytest.param(lambda d: d["safety"].pop("fabricated") and None, id="counter-dropped"),
        pytest.param(lambda d: d["code"].update(dirty=False), id="no-code-dirty"),
        pytest.param(lambda d: d.update(schema="agentic-sre.product-run.v1"), id="schema"),
        pytest.param(lambda d: d["proof"].update(N0="PASS"), id="negative-in-proof"),
        pytest.param(lambda d: d["negatives"].update(T1="PASS"), id="proof-in-negatives"),
        pytest.param(lambda d: d["proof"].update(T1="MAYBE"), id="status"),
        pytest.param(lambda d: d["safety"].update(fabricated=-1), id="negative-counter"),
        pytest.param(lambda d: d["timeline"][0].update(verify="FAIL"), id="verify"),
        pytest.param(
            lambda d: d["timeline"][0].update(action="SetResources(service='x')"), id="repr"
        ),
        pytest.param(
            lambda d: d["revisions"][0]["requirements"][0].update(status="OPEN"),
            id="requirement-status",
        ),
        pytest.param(lambda d: d["scenario"].update(tier="SMOKE"), id="tier"),
        pytest.param(lambda d: d["scenario"].update(protocol_commit="HEAD"), id="protocol-commit"),
    ],
)
def test_the_schema_is_strict(change: Any) -> None:
    with pytest.raises(ValidationError):
        ProductRunArtifact.model_validate_json(_mutated(change))


def test_an_evaluated_artifact_has_the_same_schema() -> None:
    def evaluate(document: dict[str, Any]) -> None:
        document["proof"] = {"T1": "PASS", "T2": "FAIL"}
        document["safety"] = dict.fromkeys(document["safety"], 0)
        document["transition"] = {
            "hypothesis_key": "h",
            "rule_id": "m16.resource-pressure",
            "rule_version": "v1",
            "decisive_evidence_ids": ["e1"],
        }

    evaluated = ProductRunArtifact.model_validate_json(_mutated(evaluate))
    assert evaluated.proof[ProofId.T1] is ProofStatus.PASS


# --- the writer ------------------------------------------------------------------------


def test_run_ids_are_utc_microsecond_stamps() -> None:
    assert run_id(T0 + timedelta(microseconds=123456)) == RUN
    assert run_id(datetime(2026, 9, 26, 14, 0, tzinfo=UTC).astimezone()) == "20260926T140000000000Z"
    with pytest.raises(ValueError):
        run_id(datetime(2026, 9, 26))


def test_the_artifact_is_written_once_and_reads_back(tmp_path: Path) -> None:
    artifact = _build()
    path = write_artifact(tmp_path, RUN, artifact)
    assert path == tmp_path / RUN / "PR-02.json"
    assert load_artifact(path) == artifact
    text = path.read_text()
    with pytest.raises(ArtifactError, match="never rewritten"):
        write_artifact(tmp_path, RUN, _build(incident_id="other"))
    assert path.read_text() == text


@pytest.mark.parametrize(
    ("run", "scenario_id"),
    [("20260926T120000Z", "PR-02"), ("../x", "PR-02"), (RUN, "../PR-02"), (RUN, "a/b")],
)
def test_paths_are_refused_unless_safe(tmp_path: Path, run: str, scenario_id: str) -> None:
    from packages.evals.product.artifact import artifact_path

    with pytest.raises(ArtifactError):
        artifact_path(tmp_path, run, scenario_id)


def test_an_invalid_file_does_not_load(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(_mutated(lambda d: d.update(extra=1)))
    with pytest.raises(ArtifactError, match="not a valid"):
        load_artifact(path)


# --- code provenance --------------------------------------------------------------------


@dataclass
class Git:
    dirty: bool = False
    protocol: str = PROTOCOL
    asked: list[str] | None = None

    def head(self) -> str:
        return HEAD

    def last_commit(self, path: str) -> str:
        if self.asked is not None:
            self.asked.append(path)
        return self.protocol

    def tracked_changes(self) -> bool:
        return self.dirty


def test_provenance_is_head_and_the_protocol_documents_last_commit() -> None:
    asked: list[str] = []
    assert provenance(Git(asked=asked)) == (HEAD, PROTOCOL)
    assert asked == [PROTOCOL_DOCUMENT]
    with pytest.raises(ArtifactError, match="dirty"):
        provenance(Git(dirty=True))
    with pytest.raises(ArtifactError, match="no commit"):
        provenance(Git(protocol=""))


def test_local_git_reads_the_checkout() -> None:
    git = LocalGit(Path(ROOT))
    assert re.fullmatch(r"[0-9a-f]{40}", git.head())
    assert re.fullmatch(r"[0-9a-f]{40}", git.last_commit(PROTOCOL_DOCUMENT))
    assert isinstance(git.tracked_changes(), bool)


# --- runner ----------------------------------------------------------------------------------


def test_the_runner_hands_only_verified_actions_with_requested_offsets() -> None:
    backend = RecordingBackend(_ready_evidence())
    seen: list[RunResult] = []
    backend.write_artifact = lambda scenario, result: seen.append(result)  # type: ignore[method-assign]
    result = ProductRunner(backend).run(
        ProductScenario(
            scenario_id="s", phases=(Phase(timedelta(0), (READINESS,)),), expectation=Expectation()
        )
    )
    assert result.status is RunStatus.RUN_OK
    (handed,) = seen
    assert [(item.offset, item.action) for item in handed.timeline] == [(timedelta(0), READINESS)]
    assert all(isinstance(item, TimelineRecord) for item in handed.timeline)


def test_an_artifact_error_is_an_error_run_without_a_document() -> None:
    class Refusing(RecordingBackend):
        def write_artifact(self, scenario: ProductScenario, result: RunResult) -> None:
            super().write_artifact(scenario, result)
            raise ArtifactError("the tracked tree is dirty")

    backend = Refusing()
    result = ProductRunner(backend).run(ProductScenario("s", (), Expectation()))
    assert result.status is RunStatus.ERROR and "dirty" in (result.error or "")
    assert backend.stages()[-1] is Stage.CLUSTER_DOWN


def test_a_failed_verification_never_reaches_the_artifact() -> None:
    backend = RecordingBackend()  # nothing observed: the action cannot verify
    result = ProductRunner(backend).run(
        ProductScenario("s", (Phase(timedelta(0), (READINESS,)),), Expectation())
    )
    assert result.status is RunStatus.ERROR
    assert Stage.ARTIFACT not in backend.stages()


# --- live collection ---------------------------------------------------------------------


def _storage() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    incident = uuid4()
    with factory() as session:
        for diagnosis_id, key in ((11, "a"), (11, "b"), (13, "c"), (99, "z")):
            session.add(
                EvidenceRequirementRow(
                    requirement_key=key * 64,
                    incident_id=incident,
                    diagnosis_id=diagnosis_id,
                    hypothesis_key="h",
                    rule_id=f"rule-{key}",
                    rule_version="v1",
                    kind="RESOURCE_COVERAGE",
                    targets=[],
                    not_before=ONSET,
                    status="OPEN" if key != "a" else "SATISFIED_BY_REVISION",
                )
            )
        reads = [
            ("run-1", "CAPTURE", "SUCCESS"),
            ("run-1", "ENGINE", "SUCCESS"),
            ("run-2", "ENGINE", "ERROR"),
            ("run-3", "INVESTIGATION", "SUCCESS"),
            ("run-3", "CAPTURE", "SUCCESS"),
            ("other", "ENGINE", "SUCCESS"),
        ]
        for sequence, (run, caller, status) in enumerate(reads):
            session.add(
                InvestigationReadRow(
                    run_id=run,
                    sequence=sequence,
                    caller_class=caller,
                    capability="c",
                    query_key=f"q{sequence}",
                    query_descriptor={},
                    started_at=T0,
                    finished_at=T0,
                    committed_at=T0,
                    status=status,
                    observation=None,
                    evidence_ids=[],
                    error_type=None,
                    error_message=None,
                )
            )
        session.commit()
    return factory


def test_live_artifact_reads_associate_by_diagnosis_and_sum_the_three_runs() -> None:
    reader = LiveArtifactReader(_storage())
    requirements = reader.requirements([11, 12, 13])
    assert {key: [item.rule_id for item in value] for key, value in requirements.items()} == {
        11: ["rule-a", "rule-b"],
        13: ["rule-c"],
    }
    assert reader.provider_tape(["run-1", "run-2", "run-3"]) == ProviderTape(
        reads=5, capture_reads=2, engine_reads=2, investigation_reads=1, errors=1
    )


def _live(
    tmp_path: Path, summaries: Sequence[Mapping[str, Any]], git: Git, **fields: Any
) -> LiveBackend:
    factory = _storage()
    control = _control(Commands(), [])
    control.clock = lambda: T0 + timedelta(microseconds=123456)
    http_calls: list[tuple[str, str]] = []

    def http(method: str, url: str, headers: Mapping[str, str]) -> Any:
        http_calls.append((method, url))
        assert method == "GET" and url == "http://cp/api/v1/incidents/i1/diagnoses"
        return [dict(item) for item in summaries]

    backend = LiveBackend(
        root=ROOT,
        control_port=control,
        evidence_port=LiveEvidenceReader(factory),
        run=Commands(),
        spawn=lambda argv, env: _Process(),
        sleep=lambda _: None,
        control_plane_url="http://cp",
        http=http,
        bench_root=tmp_path,
        git=git,
        **fields,
    )
    backend._r1 = R1(
        "i1",
        11,
        ONSET,
        T0 + timedelta(minutes=2),
        (AlertActivation(LABELS, T0 + timedelta(seconds=90), T0 + timedelta(seconds=75)),),
    )
    backend._r_early = REarly(12, ONSET + timedelta(minutes=8))
    backend._r2 = 13
    backend.http_calls = http_calls  # type: ignore[attr-defined]
    return backend


def _result() -> RunResult:
    return RunResult(
        "PR-02",
        RunStatus.RUN_OK,
        timeline=(TimelineRecord(timedelta(0), DeletePodOf("payment-service"), T0),),
    )


def test_live_writes_the_run_artifact(tmp_path: Path) -> None:
    backend = _live(tmp_path, SUMMARIES, Git())
    backend.cluster_up()
    backend.write_artifact(_scenario(ProofId.T4), _result())
    assert backend.artifact_path == tmp_path / RUN / "PR-02.json"
    document = load_artifact(backend.artifact_path).document()
    assert (
        document["incident"]["id"] == "i1"
        and document["incident"]["onset"] == "2026-09-26T12:01:00Z"
    )
    assert document["provider_tape"]["reads"] == 5  # R1 + R_early + R2 runs, not "other"
    assert [len(item["requirements"]) for item in document["revisions"]] == [2, 0, 1]
    assert document["timeline"][0]["action"] == "DeletePodOf:payment-service"
    assert document["proof"] == {"T4": "NOT_EVALUATED"}
    assert {method for method, _ in backend.http_calls} == {"GET"}  # type: ignore[attr-defined]


def test_an_injected_run_id_is_shared(tmp_path: Path) -> None:
    backend = _live(tmp_path, SUMMARIES, Git(), run_id="20260101T000000000000Z")
    backend.cluster_up()
    backend.write_artifact(_scenario(), _result())
    assert backend.artifact_path == tmp_path / "20260101T000000000000Z" / "PR-02.json"


@pytest.mark.parametrize(
    ("summaries", "git", "message"),
    [
        pytest.param(SUMMARIES, Git(dirty=True), "dirty", id="dirty-tree"),
        pytest.param([*SUMMARIES, _summary(4, "MANUAL")], Git(), "accepted", id="fourth-revision"),
        pytest.param(
            [SUMMARIES[0], _summary(2, "MANUAL", diagnosis_id=20), SUMMARIES[2]],
            Git(),
            "accepted",
            id="other-revision",
        ),
        pytest.param(
            [*SUMMARIES[:2], _summary(3, "EVIDENCE_DEADLINE", run_id=None)],
            Git(),
            "run id",
            id="no-run-id",
        ),
    ],
)
def test_live_artifact_facts_that_cannot_be_established_are_errors(
    tmp_path: Path, summaries: list[dict[str, Any]], git: Git, message: str
) -> None:
    backend = _live(tmp_path, summaries, git)
    backend.cluster_up()
    with pytest.raises(ArtifactError, match=message):
        backend.write_artifact(_scenario(), _result())
    assert not any(tmp_path.rglob("*.json"))


def test_the_unresolved_tape_counter_is_null_until_evaluated() -> None:
    document = _build().document()
    assert "unresolved_tape_evidence_id" in document["safety"]
    assert document["safety"]["unresolved_tape_evidence_id"] is None
    assert len(document["safety"]) == 8  # every counter of the global safety contract
    for value in (0, 3):
        evaluated = ProductRunArtifact.model_validate_json(
            _mutated(lambda d, value=value: d["safety"].update(unresolved_tape_evidence_id=value))
        )
        assert evaluated.safety.unresolved_tape_evidence_id == value
