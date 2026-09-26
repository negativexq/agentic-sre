"""M19-6.10: R1 discovery with post-T0 provenance, one MANUAL R_early POST, scheduler-only R2."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_product_runner import ROOT, Commands, _control, _Process

from packages.evals.product.live import (
    HttpManualDiagnosis,
    LiveBackend,
    LiveEvidenceReader,
    PrometheusActivations,
)
from packages.evals.product.revisions import (
    R1,
    AlertView,
    IncidentView,
    RevisionScheduleError,
    RevisionView,
    ScheduleConfig,
    await_r2,
    check_chain,
    discover_r1,
    run_r_early,
)
from packages.evals.product.runner import (
    DRY_RUN_EPOCH,
    ProductRunner,
    RecordingBackend,
    RecordingControl,
    RunStatus,
    Stage,
)
from packages.evals.product.spec import Expectation, ProductScenario

T0 = DRY_RUN_EPOCH
ONSET = T0 + timedelta(minutes=1)
CONFIG = ScheduleConfig()
LABELS = {"alertname": "PaymentServiceErrorsHigh", "service": "payment-service", "severity": "page"}
SECOND = timedelta(seconds=1)


def _stamp(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


@dataclass
class World:
    """A control plane and Prometheus on a virtual clock; revisions appear at set times."""

    control: RecordingControl
    incidents_at: list[tuple[datetime, IncidentView]] = field(default_factory=list)
    alert_views: list[AlertView] = field(default_factory=list)
    revisions_at: list[tuple[datetime, RevisionView]] = field(default_factory=list)
    onset_value: Any = _stamp(ONSET)
    prometheus: list[Mapping[str, Any]] = field(default_factory=list)
    manual_creates: list[RevisionView] = field(default_factory=list)
    posts: list[tuple[str, datetime]] = field(default_factory=list)
    reads: int = 0

    def incidents(self) -> Sequence[IncidentView]:
        self.reads += 1
        return [item for at, item in self.incidents_at if self.control.now() >= at]

    def alerts(self, incident_id: str) -> Sequence[AlertView]:
        return self.alert_views

    def revisions(self, incident_id: str) -> Sequence[RevisionView]:
        self.reads += 1
        return [item for at, item in self.revisions_at if self.control.now() >= at]

    def onset(self, incident_id: str, revision_number: int) -> Any:
        return self.onset_value

    def active_alerts(self) -> Sequence[Mapping[str, Any]]:
        return self.prometheus

    def post_manual(self, incident_id: str) -> None:
        self.posts.append((incident_id, self.control.now()))
        for revision in self.manual_creates:
            self.revisions_at.append((self.control.now(), revision))


INITIAL = RevisionView(11, 1, "INITIAL", None)
MANUAL = RevisionView(12, 2, "MANUAL", 11)
DEADLINE = RevisionView(13, 3, "EVIDENCE_DEADLINE", 12)


def _world(*, incident_at: datetime = T0 + timedelta(minutes=2)) -> World:
    world = World(RecordingControl([], start=T0))
    world.incidents_at = [(incident_at, IncidentView("i1", incident_at))]
    world.alert_views = [AlertView(dict(LABELS), T0 + timedelta(seconds=90))]
    world.revisions_at = [(incident_at, INITIAL)]
    world.prometheus = [
        {"labels": dict(LABELS), "activeAt": _stamp(T0 + timedelta(seconds=75)), "state": "firing"}
    ]
    world.manual_creates = [MANUAL]
    return world


def _discover(world: World) -> R1:
    return discover_r1(world, world, world.control, t0=T0, config=CONFIG)


# --- R1 discovery --------------------------------------------------------------------------


def test_r1_is_discovered_by_reads_only_with_exact_activation() -> None:
    world = _world()
    r1 = _discover(world)
    assert (r1.incident_id, r1.diagnosis_id, r1.onset) == ("i1", 11, ONSET)
    (activation,) = r1.activations
    assert activation.active_at == T0 + timedelta(seconds=75)
    assert world.posts == []
    assert r1.discovered_at >= T0 + timedelta(minutes=2)


def test_r1_waits_for_the_initial_revision() -> None:
    world = _world()
    world.revisions_at = [(T0 + timedelta(minutes=4), INITIAL)]
    assert _discover(world).discovered_at >= T0 + timedelta(minutes=4)


@pytest.mark.parametrize(
    ("appears", "found"),
    [
        pytest.param(T0 + timedelta(minutes=10), True, id="at-T0+10m"),
        pytest.param(T0 + timedelta(minutes=10) + SECOND, False, id="after-T0+10m"),
    ],
)
def test_no_r1_within_ten_minutes_is_an_error(appears: datetime, found: bool) -> None:
    world = _world(incident_at=appears)
    if found:
        assert _discover(world).incident_id == "i1"
        return
    with pytest.raises(RevisionScheduleError, match="no R1"):
        _discover(world)
    assert world.control.now() == T0 + timedelta(minutes=10)


def test_more_than_one_incident_is_an_error_not_a_choice() -> None:
    world = _world()
    world.incidents_at.append(
        (T0 + timedelta(minutes=2), IncidentView("i2", T0 + timedelta(minutes=2)))
    )
    with pytest.raises(RevisionScheduleError, match="2 incidents"):
        _discover(world)


def test_an_incident_before_t0_is_an_error() -> None:
    world = _world()
    world.incidents_at = [(T0, IncidentView("early", T0 - SECOND))]
    with pytest.raises(RevisionScheduleError, match="before T0"):
        _discover(world)


def _prometheus(world: World, **alert: Any) -> None:
    world.prometheus = [{"labels": dict(LABELS), **alert}]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(
            lambda w: _prometheus(w, activeAt=_stamp(T0 - SECOND)),
            "pre-history",
            id="active-before-T0",
        ),
        pytest.param(lambda w: _prometheus(w), "activeAt is missing", id="activeAt-missing"),
        pytest.param(
            lambda w: _prometheus(w, activeAt="yesterday"), "not a timestamp", id="malformed"
        ),
        pytest.param(
            lambda w: _prometheus(w, activeAt="2026-01-01T00:01:00"), "no timezone", id="naive"
        ),
        pytest.param(
            lambda w: _prometheus(w, activeAt=_stamp(T0 + timedelta(seconds=91))),
            "newer occurrence",
            id="newer-occurrence",
        ),
        pytest.param(lambda w: setattr(w, "prometheus", []), "0 active", id="no-match"),
        pytest.param(
            lambda w: setattr(w, "prometheus", w.prometheus * 2), "2 active", id="ambiguous"
        ),
        pytest.param(
            lambda w: setattr(
                w,
                "prometheus",
                [
                    {
                        "labels": {**LABELS, "severity": "ticket"},
                        "activeAt": _stamp(T0 + timedelta(seconds=75)),
                    }
                ],
            ),
            "0 active",
            id="name-alone-is-not-identity",
        ),
        pytest.param(lambda w: setattr(w, "alert_views", []), "no alerts", id="no-alerts"),
        pytest.param(lambda w: setattr(w, "onset_value", None), "onset is missing", id="no-onset"),
        pytest.param(
            lambda w: setattr(w, "onset_value", _stamp(T0 - SECOND)),
            "before T0",
            id="onset-before-T0",
        ),
    ],
)
def test_unproven_provenance_is_an_error(mutate: Any, message: str) -> None:
    world = _world()
    mutate(world)
    with pytest.raises(RevisionScheduleError, match=message):
        _discover(world)


def test_activation_exactly_at_t0_and_equal_to_starts_at_qualifies() -> None:
    world = _world()
    world.alert_views = [AlertView(dict(LABELS), T0)]
    _prometheus(world, activeAt=_stamp(T0))
    world.onset_value = _stamp(T0)
    assert _discover(world).onset == T0


def test_every_linked_alert_needs_its_own_activation() -> None:
    world = _world()
    other = {**LABELS, "alertname": "OrderServiceErrorsHigh"}
    world.alert_views.append(AlertView(other, T0 + timedelta(seconds=90)))
    with pytest.raises(RevisionScheduleError, match="OrderServiceErrorsHigh"):
        _discover(world)
    world.prometheus.append({"labels": other, "activeAt": _stamp(T0 - SECOND)})
    with pytest.raises(RevisionScheduleError, match="pre-history"):
        _discover(world)


@pytest.mark.parametrize(
    "first",
    [
        pytest.param(RevisionView(11, 1, "MANUAL", None), id="not-INITIAL"),
        pytest.param(RevisionView(11, 1, "INITIAL", 7), id="has-previous"),
        pytest.param(RevisionView(11, 2, "INITIAL", None), id="number-not-1"),
    ],
)
def test_revision_one_must_be_a_fresh_initial(first: RevisionView) -> None:
    world = _world()
    world.revisions_at = [(T0, first)]
    with pytest.raises(RevisionScheduleError):
        _discover(world)


def test_r1_with_a_later_revision_already_present_is_an_error() -> None:
    world = _world()
    world.revisions_at.append((T0, MANUAL))
    with pytest.raises(RevisionScheduleError, match="unexpected revision 2"):
        _discover(world)


# --- R_early -----------------------------------------------------------------------------


def _r1(world: World) -> R1:
    return _discover(world)


def test_r_early_posts_once_at_onset_plus_eight_minutes() -> None:
    world = _world()
    r_early = run_r_early(_r1(world), world, world, world.control, config=CONFIG)
    assert world.posts == [("i1", ONSET + timedelta(minutes=8))]
    assert (r_early.diagnosis_id, r_early.posted_at) == (12, ONSET + timedelta(minutes=8))


def test_r_early_exactly_at_the_target_is_on_time() -> None:
    world = _world()
    r1 = _r1(world)
    world.control.wait(ONSET + timedelta(minutes=8) - world.control.now())
    run_r_early(r1, world, world, world.control, config=CONFIG)
    assert len(world.posts) == 1


def test_r1_found_after_the_r_early_target_is_an_error_without_a_post() -> None:
    world = _world()
    r1 = _r1(world)
    world.control.wait(ONSET + timedelta(minutes=8) + SECOND - world.control.now())
    with pytest.raises(RevisionScheduleError, match="already passed"):
        run_r_early(r1, world, world, world.control, config=CONFIG)
    assert world.posts == []


@pytest.mark.parametrize(
    ("creates", "message"),
    [
        pytest.param([], "no revision 2", id="no-revision"),
        pytest.param([RevisionView(12, 2, "MANUAL", 99)], "links to 99", id="wrong-previous"),
        pytest.param([RevisionView(12, 2, "INITIAL", 11)], "expected MANUAL", id="wrong-trigger"),
        pytest.param([RevisionView(12, 3, "MANUAL", 11)], "not 1..n", id="gap"),
        pytest.param(
            [MANUAL, RevisionView(13, 3, "MANUAL", 12)], "expected EVIDENCE", id="duplicate"
        ),
    ],
)
def test_the_manual_post_must_become_revision_two(
    creates: list[RevisionView], message: str
) -> None:
    world = _world()
    world.manual_creates = creates
    with pytest.raises(RevisionScheduleError, match=message):
        run_r_early(_r1(world), world, world, world.control, config=CONFIG)


def test_a_deadline_revision_before_manual_is_an_error_and_prevents_the_post() -> None:
    world = _world()
    r1 = _r1(world)
    world.revisions_at.append(
        (ONSET + timedelta(minutes=5), RevisionView(12, 2, "EVIDENCE_DEADLINE", 11))
    )
    world.control.wait(ONSET + timedelta(minutes=6) - world.control.now())
    with pytest.raises(RevisionScheduleError):
        run_r_early(r1, world, world, world.control, config=CONFIG)
    assert world.posts == []


# --- R2 ------------------------------------------------------------------------------------


def _after_r_early(world: World) -> R1:
    r1 = _r1(world)
    run_r_early(r1, world, world, world.control, config=CONFIG)
    return r1


@pytest.mark.parametrize(
    ("appears", "found"),
    [
        pytest.param(ONSET + timedelta(minutes=20), True, id="before-deadline"),
        pytest.param(ONSET + timedelta(minutes=25), True, id="at-deadline-final-read"),
        pytest.param(ONSET + timedelta(minutes=25) + SECOND, False, id="after-deadline"),
    ],
)
def test_r2_is_read_until_onset_plus_25_minutes(appears: datetime, found: bool) -> None:
    world = _world()
    r1 = _after_r_early(world)
    world.revisions_at.append((appears, DEADLINE))
    if found:
        assert await_r2(r1, world, world.control, config=CONFIG) == 13
    else:
        with pytest.raises(RevisionScheduleError, match="no EVIDENCE_DEADLINE"):
            await_r2(r1, world, world.control, config=CONFIG)
        assert world.control.now() == ONSET + timedelta(minutes=25)
    assert len(world.posts) == 1  # R2 never posts


@pytest.mark.parametrize(
    "third",
    [
        pytest.param(RevisionView(13, 3, "MANUAL", 12), id="duplicate-manual"),
        pytest.param(RevisionView(13, 3, "EVIDENCE_DEADLINE", 11), id="links-to-R1"),
        pytest.param(RevisionView(13, 4, "EVIDENCE_DEADLINE", 12), id="gap"),
    ],
)
def test_r2_must_be_the_deadline_revision_after_manual(third: RevisionView) -> None:
    world = _world()
    r1 = _after_r_early(world)
    world.revisions_at.append((ONSET + timedelta(minutes=20), third))
    with pytest.raises(RevisionScheduleError):
        await_r2(r1, world, world.control, config=CONFIG)


def test_a_fourth_revision_is_an_error() -> None:
    world = _world()
    r1 = _after_r_early(world)
    world.revisions_at.append((ONSET + timedelta(minutes=20), DEADLINE))
    world.revisions_at.append((ONSET + timedelta(minutes=20), RevisionView(14, 4, "MANUAL", 13)))
    with pytest.raises(RevisionScheduleError, match="unexpected revision 4"):
        await_r2(r1, world, world.control, config=CONFIG)


def test_the_full_chain_is_accepted() -> None:
    assert [
        item.revision_number for item in check_chain([DEADLINE, INITIAL, MANUAL], at_most=3)
    ] == [
        1,
        2,
        3,
    ]


# --- runner mapping ----------------------------------------------------------------------------


def _scenario() -> ProductScenario:
    return ProductScenario(scenario_id="s", phases=(), expectation=Expectation())


class Refusing(RecordingBackend):
    def run_r_early(self, scenario: ProductScenario) -> None:
        super().run_r_early(scenario)
        raise RevisionScheduleError("R_early target already passed")


def test_a_schedule_error_is_an_error_run_with_traffic_off_and_teardown() -> None:
    backend = Refusing()
    result = ProductRunner(backend).run(_scenario())
    assert result.status is RunStatus.ERROR and "already passed" in (result.error or "")
    stages = backend.stages()
    assert Stage.AWAIT_R2 not in stages and Stage.ARTIFACT not in stages
    assert stages[-1] is Stage.CLUSTER_DOWN
    toggles = [event for event in backend.events if event[0] == "set_traffic"]
    assert toggles[-1] == ("set_traffic", False)


def test_the_backend_learns_t0_before_traffic_starts() -> None:
    backend = RecordingBackend()
    ProductRunner(backend).run(_scenario())
    kinds = [event[0] for event in backend.events]
    assert kinds.index("at_t0") < backend.events.index(("set_traffic", True))
    assert ("at_t0", DRY_RUN_EPOCH) in backend.events


# --- live ports and the POST boundary -------------------------------------------------------


@dataclass
class Http:
    """A recording HTTP client answering from the ``World``."""

    world: World
    calls: list[tuple[str, str, str | None, str]] = field(default_factory=list)
    stage: str = ""

    def __call__(self, method: str, url: str, headers: Mapping[str, str]) -> Any:
        parts = urlsplit(url)
        trigger = parse_qs(parts.query).get("trigger", [None])[0]
        self.calls.append((self.stage, method, trigger, parts.path))
        path = parts.path
        if method == "POST":
            assert path == "/api/v1/incidents/i1/diagnosis"
            self.world.post_manual("i1")
            return {}
        if path == "/api/v1/alerts":
            return {"status": "success", "data": {"alerts": list(self.world.prometheus)}}
        if path == "/api/v1/incidents":
            return [
                {"incident_id": item.incident_id, "created_at": _stamp(item.created_at)}
                for item in self.world.incidents()
            ]
        if path == "/api/v1/incidents/i1/alerts":
            return [
                {"labels": dict(item.labels), "starts_at": _stamp(item.starts_at)}
                for item in self.world.alert_views
            ]
        if path == "/api/v1/incidents/i1/diagnoses":
            return [
                {
                    "diagnosis_id": item.diagnosis_id,
                    "revision_number": item.revision_number,
                    "trigger": item.trigger,
                    "previous_diagnosis_id": item.previous_diagnosis_id,
                }
                for item in self.world.revisions("i1")
            ]
        if path == "/api/v1/incidents/i1/diagnoses/1":
            return {"diagnosis": {"symptoms": {"onset": self.world.onset_value}}}
        raise AssertionError(f"unexpected {method} {url}")


def _live(world: World, http: Http, probes: list[str], spawned: list[list[str]]) -> LiveBackend:
    def spawn(argv: Sequence[str], env: Mapping[str, str]) -> _Process:
        spawned.append(list(argv))
        return _Process()

    def probe(
        url: str, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> Mapping[str, Any]:
        probes.append(url)
        return {
            "initiating_finding_count": 0,
            "initiating_finding_ids": [],
            "root_eligible_manifestation_only_count": 0,
            "root_eligible_manifestation_only_hypothesis_ids": [],
        }

    control = _control(Commands(), [])
    control.clock = world.control.now
    control.sleep = lambda seconds: world.control.wait(timedelta(seconds=seconds))
    return LiveBackend(
        root=ROOT,
        control_port=control,
        evidence_port=LiveEvidenceReader.__new__(LiveEvidenceReader),
        run=Commands(),
        spawn=spawn,
        sleep=lambda _: None,
        control_plane_url="http://cp",
        api_token="t0ken",
        request_json=probe,
        prometheus_port=19090,
        port_ready=lambda port: True,
        http=http,
    )


def test_live_schedule_posts_one_manual_diagnosis_and_only_in_r_early() -> None:
    world = _world()
    world.revisions_at.append((ONSET + timedelta(minutes=20), DEADLINE))
    http = Http(world)
    probes: list[str] = []
    spawned: list[list[str]] = []
    backend = _live(world, http, probes, spawned)
    backend.start_fresh_db_and_control_plane()
    backend.clean_baseline(_scenario())
    backend.at_t0(T0)
    for stage, step in (
        ("R1", backend.await_r1),
        ("R_EARLY", backend.run_r_early),
        ("R2", backend.await_r2),
    ):
        http.stage = stage
        step(_scenario())
    posts = [call for call in http.calls if call[1] == "POST"]
    assert posts == [("R_EARLY", "POST", "MANUAL", "/api/v1/incidents/i1/diagnosis")]
    assert {call[1] for call in http.calls if call[0] in ("R1", "R2")} == {"GET"}
    assert probes == ["http://cp/api/v1/baseline-probe"]  # the M19-6.8 probe POST stays allowed
    assert ("R1", "GET", None, "/api/v1/alerts") in http.calls
    assert world.posts == [("i1", ONSET + timedelta(minutes=8))]
    assert any(
        "observability" in argv and "service/prometheus" in argv and "19090:9090" in argv
        for argv in spawned
    )


def test_live_r1_without_t0_or_prometheus_refuses() -> None:
    world = _world()
    backend = _live(world, Http(world), [], [])
    with pytest.raises(RuntimeError, match="T0"):
        backend.await_r1(_scenario())
    backend.at_t0(T0)
    backend.prometheus_port = None
    with pytest.raises(RuntimeError, match="Prometheus"):
        backend.await_r1(_scenario())


def test_the_manual_port_refuses_a_second_post() -> None:
    calls: list[str] = []
    port = HttpManualDiagnosis("http://cp", {}, lambda method, url, headers: calls.append(url))
    port.post_manual("i1")
    with pytest.raises(RevisionScheduleError, match="second"):
        port.post_manual("i1")
    assert calls == ["http://cp/api/v1/incidents/i1/diagnosis?trigger=MANUAL"]


@pytest.mark.parametrize(
    "document",
    [
        {"status": "error"},
        {"status": "success", "data": {}},
        {"status": "success", "data": {"alerts": [1]}},
    ],
)
def test_a_malformed_prometheus_answer_is_an_error(document: Any) -> None:
    port = PrometheusActivations("http://prom", lambda method, url, headers: document)
    with pytest.raises(RevisionScheduleError):
        port.active_alerts()


PRODUCT = Path(ROOT) / "packages" / "evals" / "product"


def _owners(predicate: Any) -> set[str]:
    """``module:Class.function`` names whose body satisfies ``predicate`` for some node."""
    found: set[str] = set()

    def visit(module: str, node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef):
                visit(module, child, (*scope, child.name))
                continue
            if predicate(child):
                found.add(f"{module}:{'.'.join(scope)}")
            visit(module, child, scope)

    for path in sorted(PRODUCT.glob("*.py")):
        visit(path.stem, ast.parse(path.read_text()), ())
    return found


def test_diagnosis_post_authority_lives_only_in_the_r_early_helper() -> None:
    def calls(name: str) -> Any:
        return lambda node: (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == name)
                or (isinstance(node.func, ast.Name) and node.func.id == name)
            )
        )

    diagnosis_url = _owners(
        lambda node: (
            isinstance(node, ast.Constant | ast.JoinedStr) and "/diagnosis" in ast.unparse(node)
        )
    )
    assert diagnosis_url == {"live:HttpManualDiagnosis.post_manual"}
    assert _owners(calls("post_manual")) == {"revisions:run_r_early"}
    assert _owners(calls("HttpManualDiagnosis")) == {"live:LiveBackend.run_r_early"}
    assert _owners(calls("run_r_early")) >= {"live:LiveBackend.run_r_early"}
    assert "live:LiveBackend.await_r1" not in _owners(calls("run_r_early"))
