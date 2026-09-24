# M18A Runtime Evidence Contract

Contract version: m18a.v1  
Status: **FROZEN**  
Frozen at commit: bb66849e9ee6157239003a2de4565d3bf7749813  
Scope: M18A runtime evidence acquisition and normalization only.

This contract records the repository architecture inspected at the freeze
commit and defines the minimum compatible runtime evidence path for M18A.
Implementations must conform to this document. A material contradiction
requires an explicit contract amendment before production implementation.

## Architecture and authority

The current diagnosis build already loads source trace spans, canonicalizes
them in runtime_graph.py, derives RuntimeEvidence, and feeds typed runtime
propagation into deterministic RCA context. Investigation is a separate path:
bounded semantic tools return InvestigationObservation; the graph normalizes
that observation, appends Findings, then rebuilds the deterministic case.

The M18A authority boundary is:

    gap and authorized semantic capability
    → bounded backend adapter and trusted query compiler
    → typed pillar observation
    → deterministic normalizer
    → typed Finding where an existing Finding family applies
    → existing deterministic RCA rebuild

The adapter acquires. The normalizer classifies. The RCA engine interprets.
The planner selects a bounded read and does not authorize it. The LLM has no
runtime evidence or RCA authority. M18A adds no hypothesis-elimination,
contradiction, confidence, root-selection, or resolution semantics.

## Current-path matrix at freeze

| Capability | Gap dimension(s) | Candidate / authorization source | Backend and query compiler | Typed raw observation | Current normalizer / Finding | Current RCA consumption | Status |
|---|---|---|---|---|---|---|---|
| resource_pressure | RESOURCE_PRESSURE, METRIC_BASELINE | information_gap.py capability and actor-local mapping; exact AuthorizedQuery; actions.py checks gap/capability/target | PrometheusInvestigationBackend → PrometheusMetricsReader; trusted memory working-set and CPU throttling PromQL; Pod-only | ResourcePressure (baseline, peak, at, resource/container, evidence ID) | normalizers.py calls resource_findings; only threshold-crossing, baseline-backed pressure becomes RESOURCE_PRESSURE Finding | Finding enters ordinary case rebuild; existing manifestation scoring can consume it | PARTIAL; normal/no-data distinction not typed |
| traffic | METRIC_CHANGE, METRIC_BASELINE | exact Service capability/target authorization | Prometheus reader; trusted http_requests_total rate query | TrafficObservation time series | traffic_findings creates TRAFFIC_INCREASE only when a sampled post-onset rate crosses the fixed 1.5x baseline rule | Finding enters ordinary deterministic rebuild | PARTIAL; no generic error/latency/onset observations |
| logs | LOG_ERROR_PATTERN, DEPENDENCY_HEALTH | gap contracts map authorized Service/Pod/workload targets; exact validation before tool invocation | LokiLogReader builds bounded service-name LogQL; LokiInvestigationBackend currently delegates logs to base source | LogRecord (service, timestamp, severity, bounded message, evidence ID) | normalizers.py calls dependency_findings; deterministic connection-error regex plus topology relation can produce DEPENDENCY_ERRORS | Finding enters ordinary deterministic rebuild | PARTIAL; generic raw logs exist, but no typed mechanism match or query provenance |
| runtime_traces | FAILURE_ONSET, DEPENDENCY_HEALTH | information_gap.py authorizes runtime trace reads for supported Pod/controller actors and open dependency alternatives; exact candidate/discriminator and action validation | TempoInvestigationBackend → TempoTraceReader; target-scoped trusted TraceQL; bounded time, trace count and response bytes | TraceSpanObservation with trace/span/parent IDs, service, kind, timestamps, status, allowlisted attributes and evidence ID | Tool returns payload.traces; normalizers.py has no trace branch, so no investigation Finding is generated | Initial build separately uses runtime_evidence.py, runtime_graph.py, and runtime_propagation.py; investigation graph does not pass acquired spans into that path | NORMALIZER_MISSING |
| Kubernetes source history / events | change, event, onset and other existing dimensions | existing gap contracts and exact action validation | SourceInvestigationBackend, journal query | ObjectVersion / ClusterEvent | existing deterministic signal normalizers | ordinary rebuild | COMPLETE for existing source semantics; outside M18A changes |

Authorization is the InformationGap.authorized_queries pair of semantic
capability and exact target. validate_action rejects a capability/target not
listed for the selected resolvable gap before a tool can run. Candidate legality
and query bounds remain separate from authorization. Planning never creates
execution authority.

### Existing backend bounds

Prometheus compiles only fixed metric templates. Query windows are timezone
aware, ordered, and at most 3600 seconds; result point limits are at most 32;
response bytes are capped at 2 MiB; timeout is configured in the bounded
0.1–30 second range. Tempo compiles a fixed target-scoped TraceQL selector;
windows are at most 3600 seconds, query limit is at most 32, search is bounded
to 8 trace IDs, and search/fetch response sizes are capped. Loki compiles its
service selector and error pattern internally; its current reader uses a
configured timeout and result limit. M18A must enforce the effective target,
time, result, timeout, and budget constraints in the server-side adapter path
and record them in provenance; caller-supplied native query text is forbidden.

## Current runtime evidence versus missing routes

| Pillar / semantic family | Existing raw acquisition | Existing typed input | Existing deterministic signal | Missing M18A route |
|---|---|---|---|---|
| Prometheus memory and CPU pressure | yes, fixed PromQL by Pod | ResourcePressure samples | abnormal pressure Finding under fixed thresholds and baseline rule | explicit observation status and provenance envelope; actual sampled-normal state; NO_DATA separate from empty/invalid data |
| Prometheus traffic change | yes, fixed request-rate PromQL by Service | TrafficObservation samples | TRAFFIC_INCREASE under fixed baseline comparison | shared status/provenance envelope; no-data explicit; current capability does not cover arbitrary latency/error families |
| Loki dependency/error | live LokiLogReader exists; investigation backend presently delegates logs to source | LogRecord | existing connection-error regex and topology-conditioned DEPENDENCY_ERRORS | live Loki reader routed into investigation; typed mechanism match; normalized query provenance; unmatched text remains neutral |
| Tempo caller/callee and outcomes | live bounded Tempo reader and target context selection | TraceSpanObservation; initial path already builds RuntimeEvidence and propagation structures | initial diagnosis has deterministic trace evidence; investigation normalizer emits none | connect acquired investigation spans to deterministic typed runtime observations and Finding normalization; per-read provenance; preserve direction and outcome without assigning root |

Existing runtime_evidence.py already classifies span protocol/status and
aggregates service and strict parent-child call outcomes. runtime_graph.py
derives direction from explicit parent/child and span-kind relations.
runtime_propagation.py derives observed propagation metadata. These modules
are observational evidence products; the initial diagnosis uses them, but the
investigation observation normalizer does not. M18A reuses these semantics
where applicable instead of building a parallel trace graph. Tempo currently
does not populate duration_raw; elapsed duration can be computed only when
both timestamps are present and ordered. No critical-path or “first failing
hop” claim is frozen unless its deterministic source and exact rule are
available and tested.

### Pillar capability audit

| Requested family | Repository support at freeze | Contract treatment |
|---|---|---|
| Prometheus CPU/memory pressure | fixed Pod-scoped working-set/limit and CPU throttling query templates; typed ResourcePressure; deterministic abnormal thresholds | M18A typed status/provenance; preserve thresholds; normal only from actual sample coverage |
| Prometheus traffic change | fixed Service-scoped HTTP request-rate template; typed TrafficObservation; existing onset/baseline increase rule | admitted for METRIC_CHANGE and METRIC_BASELINE |
| Prometheus latency onset / error onset | workload metrics and dashboard/alert expressions exist, but investigation reader has no bounded semantic query templates or normalizer for them | deferred; dashboard PromQL is not an investigation capability |
| Prometheus upstream/downstream temporal ordering | no investigation query or typed state-change route; relation data exists in trace propagation structures instead | deferred to Tempo-derived facts where parent/child evidence supports it |
| Loki dependency timeout / refusal | current log query selects generic error-like lines and the shared connection-error regex recognizes a broad connection-error family; dependency_findings needs topology context | existing generic deterministic error path can be reused; M18A may add only explicit typed subcategories with tests |
| Loki pool exhaustion / database acquisition delay | no distinct investigation normalizer or typed category at freeze | deferred until a deterministic structured pattern is present and tested |
| Loki propagation onset | no normalized caller/callee direction from logs | deferred; Tempo parent/child relation is the admitted source for direction |
| Tempo caller/callee and request direction | provider-neutral TraceSpanObservation plus strict parent-child/span-kind classifier and RuntimeEvidence call summaries exist in initial diagnosis | reuse for investigation normalization; never interpret direction as root cause |
| Tempo error state | explicit span status and protocol status/error attributes are classified by RuntimeSpanOutcome | admitted as typed observed outcome; Finding only through deterministic existing-consumer-compatible mapping |
| Tempo latency / local and child duration | timestamps exist; duration_raw is not populated by the Tempo parser; runtime propagation does not define a latency threshold | expose measured elapsed duration only from ordered timestamps; abnormality/latency-introduction claims deferred absent a frozen deterministic rule |
| Tempo critical path / first failing hop | no established investigation normalizer or current Finding mapping proving these exact claims | deferred; do not infer from a single downstream error |

## InformationGap → runtime capability contract

| Gap dimension | Allowed runtime capability | Permitted observation family | Existing / M18A Finding family | Status / restriction |
|---|---|---|---|---|
| RESOURCE_PRESSURE | resource_pressure | Pod memory working-set ratio; CPU throttled-period ratio | existing RESOURCE_PRESSURE for abnormal threshold-crossing samples | Normal observations are typed state only in M18A; no pressure Finding for normal. Empty samples are NO_DATA. |
| METRIC_BASELINE | resource_pressure, traffic | same bounded resource or request-rate measurements | existing pressure/increase Finding only when existing deterministic rules match | Does not authorize arbitrary metrics. |
| METRIC_CHANGE | traffic | request-rate change before/after onset | existing TRAFFIC_INCREASE | No latency/error Finding is implied by generic metric access. |
| LOG_ERROR_PATTERN | logs | recognized, allowlisted dependency/error mechanism pattern | existing DEPENDENCY_ERRORS only through deterministic normalizer and topology relation | Raw or unmatched text is neutral. |
| DEPENDENCY_HEALTH | logs, runtime_traces | dependency error pattern; target-scoped span status/latency and caller/callee relation | existing DEPENDENCY_ERRORS for logs; typed trace runtime Finding using only an already-consumed Finding family, if mapping is deterministic | A service being queried is not sufficient: the gap must explicitly authorize the capability/target. |
| FAILURE_ONSET | runtime_traces | bounded span start/end, error/latency status and parent-child timing | typed runtime trace Finding only for a supported deterministic observation | No root actor or elimination claim. |
| EVENT_SEQUENCE, CHANGE_TIMING | none from runtime pillars in m18a.v1 | existing Kubernetes events, history, incident_events, incident_changes only | existing Finding families | Runtime telemetry is not made a wildcard discovery source. |
| all other dimensions | none unless listed above | none | none | Capability-to-gap relation remains explicit and narrow. |

Gap mappings are additive only where listed. M18A does not alter M15's
discovery discriminator or planner ranking. Runtime-unavailable behavior must
remain equivalent to the existing source-only behavior.

## Frozen typed contracts

Reuse existing InvestigationObservation, ResourcePressure,
TrafficObservation, LogRecord, TraceSpanObservation, and Finding models.
Task 2 may add a small shared runtime envelope and pillar-specific typed facts
only where the existing models cannot express the requirements below; it must
not create a second hypothesis or RCA model hierarchy.

Every runtime investigation observation must explicitly carry:

| Field | Contract |
|---|---|
| pillar | PROMETHEUS, LOKI, or TEMPO |
| capability | exact semantic capability (resource_pressure, traffic, logs, or runtime_traces) |
| target | canonical authorized EntityRef |
| time scope | timezone-aware requested and effective start/end; effective range clipped to source observation cutoff |
| outcome state | UNKNOWN, NO_DATA, OBSERVED_NORMAL, or OBSERVED_ABNORMAL; mutually distinct |
| typed facts | validated existing source model or a minimal typed extension; never free-form model claims |
| provenance | backend pillar, semantic capability, canonical target, effective interval, trusted compiled-query descriptor/identity, source observation IDs, and normalization rule/version |

NO_DATA is emitted only when a successful bounded query returns no usable
samples/records. Backend/protocol errors remain UNKNOWN with error metadata;
they are not empty healthy results. OBSERVED_NORMAL requires real samples and
a named deterministic rule whose normal range is established in code/config.
Missing samples, invalid samples, partial windows, or an unavailable backend
cannot produce OBSERVED_NORMAL.

### Prometheus facts

The minimum typed metric fact carries metric family/template identity,
canonical target, effective time window, measured summary and units, sample
count, baseline/reference where the existing rule requires one, threshold or
rule identifier, explicit observation state, and source evidence IDs. The
existing ResourcePressure/TrafficObservation fields remain the base facts.
The existing resource pressure thresholds (memory >= 0.90, CPU throttling
>= 0.25, and baseline factor 0.60) and traffic factor (1.5) remain
unchanged. A real measured normal resource result becomes a typed
OBSERVED_NORMAL observation; it does not become a RESOURCE_PRESSURE Finding
and does not create new elimination semantics. Existing abnormal
RESOURCE_PRESSURE and TRAFFIC_INCREASE Findings remain under existing
normalizers.

### Loki facts

The minimum typed log fact carries canonical service/target, timestamp,
optional deterministically resolved peer, mechanism category, matched
structured-pattern identifier, bounded time scope, source evidence ID, and
provenance. M18A may classify only explicit patterns implemented and tested
in the deterministic normalizer. The current _CONNECTION_ERROR regex and
topology-dependent dependency_findings path are reusable. Raw message text
may be retained as bounded provenance/context; raw text alone never changes
hypothesis or alternative state. Unrecognized text is UNKNOWN/neutral, and
empty successful query is NO_DATA.

### Tempo facts

Reuse TraceSpanObservation and the existing canonical trace/evidence
derivation. The typed per-read facts retain trace/span IDs, parent ID, service,
span kind, start/end, status, allowlisted semantic attributes, evidence ID,
and deterministically derived caller/callee direction where a parent-child
relation is supported. Duration is derived only from ordered timestamps.
Error/latency state, local-versus-child duration, critical-path relation and
first-failing-hop are emitted only when existing source fields support the
deterministic rule; otherwise they are deferred. A span does not name a root
actor. Missing spans in a successful bounded read are NO_DATA, not normal.

## Finding and provenance mapping

Every Finding created from an investigation runtime observation must retain in
structured details (or an equivalent typed provenance reference):

    runtime_pillar
    semantic_capability
    canonical_target
    effective_start / effective_end
    trusted_query_descriptor_id
    source_observation_ids
    normalization_rule_id and version

The query descriptor identifies the server-owned semantic template and its
bounded parameters. It may include a stable digest of compiled query text for
debugging, but must not persist bearer tokens, authorization headers, secret
values, or arbitrary caller query text. Existing Finding families are used
when semantics match. M18A does not create a normal-state Finding that could
be misread as contradiction or root-cause evidence; Prometheus
OBSERVED_NORMAL is a typed observation until M16 defines any permissible
RCA consequence.

## Live fixture and validation plan

The repository already has a Kind observability stack in
infra/kubernetes/observability.yaml: OTEL collector, Prometheus, Loki, Tempo,
plus the sre-demo order/payment workloads. Makefile deploy waits for those
services, and workload instrumentation exposes HTTP, database and Kafka
metrics and emits telemetry. make e2e-kind is currently a bad-rollout RCA
workflow, not a per-pillar M18A proof.

M18A live validation will use the existing Kind workloads and backends, with
bounded read-only fixture queries through repository adapters:

| Pillar | Existing fixture path | Live assertion |
|---|---|---|
| Prometheus | instrumented payment-service / order-service metrics scraped by the observability stack | fixed resource/traffic semantic query returns typed measurements; one actual abnormal threshold path and one measured-normal resource path are distinguished from empty series; abnormal path normalizes to its existing Finding |
| Loki | structured workload logs exported by OTEL collector to Loki | bounded service/window query returns a deterministic recognized dependency/error record; normalizer returns the expected typed DEPENDENCY_ERRORS Finding with provenance; unmatched/empty remains neutral |
| Tempo | instrumented order→payment (and downstream) OTLP traces exported by collector | target-scoped bounded TraceQL returns typed spans; deterministic parent-child direction and error/latency observation normalizes into runtime Finding with provenance, without root/elimination authority |

Tests will include unit schema/normalizer/bounds/authority tests, investigation
integration tests, source-only regression tests, and a live Kind path for each
pillar. Mocks are valid for unit tests but cannot satisfy the live gate. No
fault injection is required solely to pass M18A; an existing instrumented
traffic path may be used. Any fixture writes needed to provision the disposable
Kind environment are setup activity only; the investigation backend actions
under test remain read-only and their write count is zero.

Focused coverage is expected in the existing RCA test areas:

- Prometheus reader and normalizer state: tests/unit/rca/test_prometheus_backend.py
  plus focused normalizer tests.
- Loki query bounds, recognized/unrecognized mechanism patterns, no-data and
  Finding provenance: focused tests adjacent to the existing log/investigation
  tests.
- Tempo parsing, span direction/outcome, target/time/result bounds and
  normalization: tests/unit/rca/test_tempo_backend.py,
  test_runtime_evidence.py, test_runtime_graph.py and focused investigation
  normalizer tests.
- InformationGap routing, exact authorization, server-side bounds,
  source-only fallback and deterministic rebuild: existing
  tests/unit/rca/test_runtime_information_gaps.py,
  test_investigation_authorization.py, test_investigation_candidates.py and
  test_investigation.py.
- Live proof: extend the existing Kind/observability infrastructure or add a
  focused non-mutating M18A live runner that uses the repository's real
  Prometheus, Loki and Tempo readers. The current scripts/kind_e2e.py exercises
  a bad-rollout lifecycle and is not itself evidence of G18A.12.

## Explicit non-goals and deferred items

- No M16 alternative-elimination semantics, new contradiction rules, root
  selection, confidence adjustment, or new RESOLVED path.
- No M15 ranking/gate/metric changes, scenario-specific behavior, or GPT use.
- No arbitrary PromQL, LogQL, TraceQL, SQL, shell, or Kubernetes commands from
  a policy or model.
- No interpretation of NO_DATA as OBSERVED_NORMAL, healthy, contradiction,
  or evidence against a hypothesis.
- No claim that Tempo downstream service is root cause; no unsupported
  critical-path/first-failing-hop derivation.
- No runtime mutation/remediation or Secret access.
- Prometheus general latency/error/temporal-order capabilities, Loki
  unrecognized-log semantics, and unsupported Tempo duration/critical-path
  facts remain deferred unless the current deterministic source fields and
  frozen contract rules prove them.
- M18B owns final end-to-end causal/recovery validation after M16 and M17.

## Freeze and amendment rule

Implementation must conform to m18a.v1. A material incompatibility must be
reported as CONTRACT_AMENDMENT_REQUIRED with the frozen assumption, contrary
code/evidence, minimal amendment, and gate impact. Do not silently edit this
contract to make implementation easier. Only trivial editorial corrections
are permitted without an amendment.
