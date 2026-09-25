# M19 Diagnosis Input Inventory

Status: M19-3.1, baseline `f4422f7` (after G19.2). Documentation only; no
behavior changes.

This inventory lists every input that `apps/control_plane/diagnosis.py::_diagnose`
and `packages/rca/live.py::LiveSource` consume today. Section 1 describes the
current code as it is, including synthetic evidence. Section 2 maps each input
to the F3 target and names the task that closes each gap. Items that do not
exist yet are marked **planned** with their task.

## 1. Current diagnosis input inventory

### 1.1 `_diagnose` direct inputs, in execution order

| # | Input | How `_diagnose` gets it | Window |
|---|---|---|---|
| D1 | Incident row | `IncidentRepository.get` (in `run`) | `updated_at` is the window end when the incident is resolved |
| D2 | Alerts | `AlertRepository.list_for_incident` (in `run`) | `min(starts_at) - 2h` opens the window (`incident_window`) |
| D3 | Clock | `DiagnosisService.clock()` | window end of an open incident; later `log_observed_at` |
| D4 | Snapshot cycle (current listing) | `snapshot_result()` → `ClusterReader.list_objects`; `cycle.objects` kept in memory; `cycle.completed_at` becomes the window end | open incidents only |
| D5 | Object journal | `ObjectVersionRepository.history(journal namespaces, starts_at, ends_at)` | window computed **before** the log-capture extension |
| D6 | Kubernetes Events | `EventRepository.analysis_view(journal namespaces, starts_at, ends_at)` | window computed **before** the log-capture extension |
| D7 | Persisted logs | `LogObservationRepository.list_for_incident` (read twice for open incidents) | final window |
| D8 | Loki capture | `capture_error_logs` → `LokiLogReader.error_logs` (≤1h slices); results written to `log_observations` | open incidents only |
| D9 | Lifecycle ledger | `LifecycleRepository.list_window(watched namespaces, starts_at, ends_at)` | final window, nothing outside it |
| D10 | Provider readers | `prometheus_reader`, `tempo_reader`, `loki_reader` passed into `LiveSource`; used lazily | `observation_cutoff()` = window end |

### 1.2 Evidence sources

Times: **source** = when the source says it happened; **observed** = when the
collector saw it; **ingested** = when the row was written.

| Source | Table / reader | Primary key | Source time | Observed time | Ingested time | Manifest `source_type` (F3) | RCA reader | Evidence id today |
|---|---|---|---|---|---|---|---|---|
| Alerts | `alerts` / `AlertRepository.list_for_incident` | `alert_id` (UUID); unique `(fingerprint, starts_at)` | `starts_at`, `ends_at` | none | none; row is updated in place on resolve | `ALERT`, content frozen in manifest (planned M19-3.6/3.6a) | `LiveSource.alerts` → `extract_symptoms`, `incident_window` | none (alerts are symptoms, not evidence ids) |
| Object journal | `object_versions` / `ObjectVersionRepository.history` | `version_id` | none (**missing**; `source_at` added by M19-3.2) | `observed_at` | **missing**; added by M19-3.2 (backfill = `observed_at`) | `OBJECT_VERSION` (planned M19-3.6) | `LiveSource.object_history` → topology, change, policy, autoscaling, container findings, A1 `_terminated` | `journal:{version_id}` |
| Current listing | in-memory `SnapshotResult.objects` (not persisted as a cycle) | none | none | window end | none | `SNAPSHOT_CYCLE` via `snapshot_cycles` / `snapshot_cycle_objects` (**planned M19-3.3/3.4**; tables do not exist yet) | `LiveSource.object_history` appends each listed body | `cluster:current` (synthetic) |
| Synthetic deletion | derived in `LiveSource.object_history` when a journal-live object is absent from the listing | none | none | window end | none | none; removed by M19-3.5 (I12) | `LiveSource.object_history` | `cluster:missing` (synthetic) |
| Kubernetes Events | `event_versions` / `EventRepository.analysis_view` | `version_id`; unique `(namespace, dedup_key)` | `event_at` (Event first/last timestamp) | `observed_at` | **missing**; added by M19-3.2 (backfill = `observed_at`) | `EVENT_VERSION` (planned M19-3.6) | `LiveSource.events` → `events_from_bodies` → failure, fault, policy, autoscaling findings; `FAILURE_EVENT` UID | `event:{Event metadata.uid}`, else `event:{list index}` (see §N.2) |
| Lifecycle ledger | `lifecycle_observations` / `LifecycleRepository.list_window` | `observation_id`; `evidence_id` unique | `source_at` | `observed_at` | `ingested_at` (present) | `LIFECYCLE` (planned M19-3.6) | `LiveSource.pod_status_observations` → `pod_status_from_lifecycle` → A1 `_recovered` | `lifecycle:{ns}:{kind}:{uid}:{seq}` |
| Change records | `change_records` / `ChangeRecordRepository` (API `/api/v1/changes`, console) | `change_id` (UUID) | `timestamp` | none | **missing**; added by M19-3.2 (backfill = `timestamp`) | `CHANGE` (planned M19-3.6) | **none**: not read by `_diagnose` or `LiveSource` (see §N.2) | — |
| Persisted logs | `log_observations` / `LogObservationRepository.list_for_incident` | `observation_id`; unique `(incident_id, dedup_key)` | `event_at` | `observed_at` | **missing**; added by M19-3.2 (backfill = `observed_at`); `source_read_id` **planned M19-3.3/3.9** | `LOG` (planned M19-3.6) | `LiveSource.error_logs` → `dependency_findings`; `LiveSource.logs` → legacy investigator tools, `SourceInvestigationBackend` | `loki:{service}:{ts}:{index}` (stored on the row) |
| Incident window metadata | `incidents` / `IncidentRepository.get` | `incident_id` | `updated_at` (resolved window end) | none | none; row is updated in place | run metadata `window_end` (planned M19-3.6) | `_diagnose` window only | — |

### 1.3 Provider reads and their callers

No provider read is persisted today, except that Loki capture results are
normalized into `log_observations`. The planned tape (`investigation_reads`,
M19-3.3) and adapter (M19-3.8/3.9) will record every row below.

| Provider | Reader call | Caller class (planned tape) | Call path | Evidence id | Persisted today |
|---|---|---|---|---|---|
| Loki | `LokiLogReader.error_logs` | `CAPTURE` | `_diagnose` → `capture_error_logs` (bulk, ≤1h slices, newest first) | `loki:{service}:{ts}:{index}` | normalized rows in `log_observations`; the read itself: no |
| Prometheus | `PrometheusMetricsReader.query_resource_pressure` | `ENGINE` | `engine.build_case` → `source.resource_pressure` → `LiveSource.resource_pressure` → A2 (`resource_findings`, `resource_mechanism`) | content digest of the result | no |
| Prometheus | same, again | `ENGINE` (inside investigation) | every investigation rebuild: `graph.rebuild_case` → `build_case(OverlayObservationSource)` → `OverlayObservationSource.resource_pressure` → base `LiveSource.resource_pressure` | content digest | no |
| Prometheus | same | `ENGINE` (legacy LLM investigator) | `tools.ResourcePressureTool` → `case.source.resource_pressure` (only with `SRE_LLM_ENABLED`) | content digest | no |
| Prometheus | `query_resource_pressure`, `query_traffic` | `INVESTIGATION` | `PrometheusInvestigationBackend` (`environment.py`) | content digest | no |
| Loki | `LokiLogReader.error_logs` | `INVESTIGATION` | `LokiInvestigationBackend.query_logs` | `loki:{service}:{ts}:{index}` | no |
| Tempo | `TempoTraceReader.query` | `INVESTIGATION` | `TempoInvestigationBackend.query_traces` | `tempo:{trace_id}:{span_id}` | no |

`LiveSource.resource_pressure` catches every reader exception and returns
nothing for that Pod (logged only): M19-3.10 turns this into a recorded
`ERROR` read.

### 1.4 `LiveSource` public methods

| Method | Returns / reads | Source row(s) above |
|---|---|---|
| `incident_id` | constructor value | D1 |
| `observation_cutoff` | `observed_at` = window end | D3/D4/D8 (window end) |
| `alerts` | `alert_items` | Alerts |
| `object_history` | journal versions + current listing + synthetic deletions | Object journal, Current listing, Synthetic deletion |
| `events` | `events_from_bodies(event_bodies)` | Kubernetes Events |
| `error_logs` | `error_items` | Persisted logs |
| `logs` | `error_items` filtered by service | Persisted logs |
| `resource_pressure` | live Prometheus reads per Pod | Provider reads: Prometheus `ENGINE` |
| `traffic_observations` | always `[]` (no bounded live reader) | none by design; live traffic only via `PrometheusInvestigationBackend` |
| `trace_observations` | always `[]` (no live trace ingestion) | none by design; live traces only via `TempoInvestigationBackend` |
| `pod_status_observations` | `pod_status_from_lifecycle(lifecycle_records)` | Lifecycle ledger |
| `supports` | capability flags from reader presence | configuration, not evidence |
| `supports_typed_runtime` | capability flags from reader presence | configuration, not evidence |
| `investigation_backend` | backend chain: Source → Prometheus → Tempo → Loki | Provider reads: `INVESTIGATION` rows |

### 1.5 Deliberately not an input

- `entity_instances` is a materialized index, updated in place, and never RCA
  evidence. `packages/rca` may not import it or read its time fields
  (`tests/unit/rca/test_entity_instance_access.py`).

## 2. F3 gaps and target-state mapping

| Gap | Current behavior | F3 target | Task |
|---|---|---|---|
| Missing ingestion provenance | `object_versions`, `event_versions`, `change_records`, `log_observations` have no `ingested_at`; `object_versions` has no `source_at` | add columns and backfill | M19-3.2 |
| Snapshot cycle not persisted | current listing lives only in memory for one diagnosis | `snapshot_cycles` + `snapshot_cycle_objects` with full bodies, written only for diagnosis capture | M19-3.3, M19-3.4 |
| Synthetic cluster evidence | `object_history` emits `cluster:current` and `cluster:missing` | history from journal + persisted snapshot cycle; deletion only from journal tombstones | M19-3.5 |
| No manifest | each source is re-queried by time window; journal/Events use the pre-extension window, logs/lifecycle the final one (§N.2) | capture → commit → one `REPEATABLE READ` manifest of exact source ids; `window_end` = capture completion | M19-3.6, M19-3.7 |
| Mutable alerts in the window | alert rows are updated on resolve | alert content frozen in the manifest | M19-3.6a |
| Provider reads not recorded | Prometheus/Loki/Tempo results are consumed directly; Prometheus ids are unresolvable digests | one `ProviderAdapter`; `investigation_reads` tape with `caller_class` `CAPTURE`/`ENGINE`/`INVESTIGATION`; persist before consume | M19-3.8, M19-3.9 |
| Swallowed provider errors | `resource_pressure` returns `[]` on failure | tape `ERROR` row; typed missing result | M19-3.10 |
| Cross-run engine reads | no run boundary on engine reads | engine reads only the current run's tape | M19-3.11 |
| Loki capture not linked to its read | `log_observations` rows carry no read id | `log_observations.source_read_id` → `investigation_reads` | M19-3.3, M19-3.9 |
| No replay | diagnosis cannot be rebuilt without live systems | `ReplaySource` from manifest + snapshot + tape | M19-3.14 – M19-3.17 |

## Acceptance checklist

- `LiveSource` public methods mapped: 14 / 14
- `_diagnose` direct inputs mapped: 10 / 10
- Unmapped sources: 0. Open findings are recorded in the M19 plan §N.2
  (Event evidence ids, window ordering, `change_records` not consumed,
  repeated `ENGINE` reads during investigation).
- Planned-only sources clearly marked: yes (`snapshot_cycles`,
  `snapshot_cycle_objects`, `run_evidence_manifest`, `investigation_reads`,
  `source_read_id`, the M19-3.2 provenance columns).
