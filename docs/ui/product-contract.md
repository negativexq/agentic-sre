# M0 — UI Product Contract

The frozen information architecture and terminology for the Agentic SRE
Operator Console. Everything downstream (frontend, API DTOs, report model,
email) is built against this contract. It is deliberately written before any
frontend code so the product surface is decided once, not drifted into.

## What the console is

An operator-facing console over the deterministic RCA core. It answers, for a
live incident:

1. What broke?
2. Which actor does the system hold responsible?
3. Why?
4. What alternatives remain?

And it turns that answer into a shareable **Incident Report** (preview, PDF,
Markdown, JSON, email).

The console is **not** a metrics dashboard, not a Grafana clone, and not a
place that computes root cause. All causal judgement comes from the engine;
the UI only renders the engine's output and its provenance.

## Primary navigation

```
Agentic SRE

Overview
Incidents
Changes
Reports
──────────────
Connections
Settings
```

`Benchmarks` is **not** in product navigation. If exposed at all it lives under
a developer route (`/dev/benchmarks`) and is never linked from the operator
surface.

## Terminology (single-meaning)

| Term | Meaning |
|---|---|
| **Root cause** | The engine's selected causal actor for a resolved diagnosis. |
| **Leading root actor** | The top-ranked actor when the diagnosis is not resolved; alternatives remain. |
| **Confidence** | `VERIFIED` / `LIKELY` / `UNVERIFIED` — how well a deterministic rule ties the actor to the symptoms. |
| **Resolution** | `RESOLVED` / `AMBIGUOUS` / `INSUFFICIENT_EVIDENCE` — whether evidence distinguishes the leading hypotheses. |
| **Finding** | One normalized deterministic signal attached to an entity (e.g. `SPEC_CHANGE`). |
| **Evidence** | The raw source observation a finding was derived from, with provenance. |
| **Investigation** | The bounded evidence-acquisition process (the trace of reads). |
| **Hypothesis** | One causal episode assembled from findings; may be `SUPPORTED` / `UNRESOLVED` / `CONTRADICTED`. |

Two invariants the UI must communicate and never violate:

- **`AMBIGUOUS ≠ WRONG`.** Ambiguous means the engine has a leading actor but
  could not positively eliminate a structural alternative. It is an honest
  epistemic state, not a failure. The UI must not color it as an error.
- **The UI never authors a causal claim.** Root actor, confidence, resolution,
  causal path, and alternatives are read verbatim from the diagnosis. The UI
  adds layout and provenance links, never interpretation.

## Confidence / resolution color semantics

Semantic tokens, decoupled from raw red/green:

```
verified    strong positive     (VERIFIED)
likely      qualified positive  (LIKELY)
unverified  neutral/muted       (UNVERIFIED)
critical    impact severity only (incident severity, not resolution)
warning     impact severity only
healthy     system-state OK
```

`AMBIGUOUS` and `INSUFFICIENT_EVIDENCE` render as **neutral/informational**,
never as `critical`. Severity color belongs to incident impact
(`CRITICAL`/`WARNING`/`INFO`), a separate axis from resolution.

## Screen contracts

### Overview
Operational "what is happening now". Top counters (active / critical /
diagnosing / median diagnosis time), an active-incident table (severity ·
incident · service · leading root actor · confidence · resolution · age),
recent changes, recent diagnoses, system health. A new operator understands
the cluster state within ~10s.

### Incidents
Filterable list (status, severity, service, confidence, resolution, time
range) with pagination. Each row links to the workspace.

### Incident Workspace (the product's core screen)
Ordered top to bottom:
1. Header: title, severity, status, service, opened-at.
2. Leading root actor + confidence + resolution badges.
3. Causal path (from the engine's `causal_path`, rendered as hops).
4. Lifecycle timeline (v2a phases bound to `diagnosis_run_id`).
5. Two columns: "Why this actor" (initiating/supporting findings) vs
   "Competing hypotheses" (alternatives with epistemic state).
6. Evidence explorer (finding · when · actor · source · summary · refs).
7. Investigation trace (the reads).

Raw JSON is never the default view; raw evidence is reachable by drill-down.

#### D3: what ran, which instance, and how firm the timing is (owner-approved 2026-10-08)

Read verbatim from the diagnosis; the UI adds no claim. Measured on the eleventh and twelfth `HOLDOUT` (160
diagnoses): timing stability is `UNASSESSED` in all 160 (live sources record no capture history yet, roadmap C7),
the leader's exact instance is `EXACT` in 78 and `UNKNOWN` in 82; the executing instance exists from engine 2.2.1
(254 replayed incidents carry it, m21 §17.6).

1. **Executed by** (m21 §17), directly under the leading actor in block 2, one line per executing instance:
   "Executed by `NetworkChaos/…-rjhbq` on `Pod/checkout-…-8rt79`, 16:34:41–16:35:36". A competing display lists them
   under each candidate. It appears only when the diagnosis carries one, so it is absent, not "missing", for a
   cause without a fired execution witness; an unknown start or end reads "time unknown", never a guess. It is
   never shown for a leader that is not shown (`NOT_ESTABLISHED`). It is a detail of the cause, not a second
   cause: no badge, no tone of its own.
2. **Instance** of the leading actor, from its causal family's `instance_resolution`, one muted line in block 2:
   `EXACT` "Exact instance: `<name>` (UID `<first 8>`)"; `MULTIPLE_VIABLE` "Several instances remain possible"; `UNKNOWN`
   "Exact instance not determined". Absent when the leader is not shown.
3. **Timing stability** (M21 timing contract §4, §5), shown only when assessed (`STABLE` or `SENSITIVE`): one line
   in block 2 ("Holds over every admissible onset" / "Depends on the exact onset"), and, when `SENSITIVE`, a
   *Timing* section in the investigation tab listing withheld authority (actor, authority, relations) and the
   status drivers per admissible onset (actor, change, reason, in the evidence's words). `UNASSESSED` shows
   nothing in block 2 and one muted line in that section ("Timing stability not assessed for this source"), so
   the operator is not shown a judgement the engine did not make.
4. **Report and list.** The exported report carries 1 and 2 beside the root cause. The incident list, overview
   and notifications stay as they are (§17.3: the notification names the root cause only).

Validation: API mapper tests per case (present, absent, unknown time, each `instance_resolution`, each timing
state); a browser check of the workspace on a live incident and on a replayed one with an executing instance.

### Changes
Change timeline (namespace / kind / actor / change type / time range). Within
an incident, changes are shown relative to onset (`-31s`, `-7m`). The
RCA-selected change is marked; the UI does not itself declare any change the
root cause.

### Reports
Immutable report snapshots. A report pins `diagnosis_run_id` and does not
change when the incident is re-diagnosed. Library with filters; per-incident a
report links back to its incident and run.

### Notifications
Announcements appear on every screen, not only the incident list. They are derived **only from persisted
incident state** (the incident list the console already refetches on each server-sent signal), never from
anything the engine did not record:

- **New incident**: an incident id this browser has not been told about (severity, service, title).
- **Diagnosis ready**: `has_diagnosis` turned true on an incident being watched (resolution, confidence and
  the leading root actor as the engine stated them; an unresolved diagnosis keeps its neutral tone). A
  re-run diagnosis is not announced again.
- The first visit of a browser takes the current state as a **silent baseline**; storage that is missing or
  corrupted reads as a first visit, so nothing is announced rather than everything. A burst shows the newest
  three and a summary of the rest.
- The navigation shows an unread count (once per incident) and the tab title carries it; both clear when the
  incident, or the incident list, is opened. Incidents already on screen are not counted as unread.
- Toasts are a polite live region, dismiss after 12 s (paused while hovered or focused) and never take focus.
  Browser (operating-system) notifications while the tab is closed are not part of this and stay deferred.

### Connections & Settings
Read-only connector health first (Kubernetes / Prometheus / Alertmanager /
Loki / Tempo / Email: Connected / Degraded / Unavailable / Not configured).
Secret values never returned to the UI.

## Scope rule (in force M0–M10)

> The RCA scoring / resolution algorithm is **not touched** while the UI and
> productization track is built. Resolver / noisy-benchmark work is a separate
> epic on its own branch. The UI renders whatever the engine currently
> produces (today: 24/25 live, 0 fabrications, 0 model calls) and must not
> change that behavior.

## G0 — gate

- [x] Six product screens defined (Overview, Incidents, Workspace, Changes, Reports, Connections/Settings).
- [x] Terminology single-meaning; `AMBIGUOUS ≠ WRONG` stated.
- [x] Benchmark UI separated from product UI.
- [x] Incident detail information order fixed.
- [x] RCA engine contract unchanged (no core edits in this milestone).

## Delivered (M1–M12)

- **M1** React/TS/Vite shell, semantic design tokens, light/dark, routing, primitives.
- **M2** `/api/v1/console/*` DTO surface + typed client; internal models never exposed; legacy API untouched.
- **M3** Overview dashboard + filterable incident list.
- **M4** Incident workspace: root actor, causal path, run-bound lifecycle, why-this-actor vs competing hypotheses, evidence & trace.
- **M5** Live updates over SSE (only persisted-state transitions; EventSource reconnect).
- **M6** Changes explorer (global + onset-relative, leading-actor marked, no UI causal claim).
- **M7** Canonical immutable `ReportSnapshot` pinned to `diagnosis_run_id`.
- **M8** Report export: PDF (fpdf2), Markdown, JSON, in-app preview.
- **M9** Reports library.
- **M10** Email sharing (SMTP when configured; audit log; idempotency; honest refusal when unconfigured).
- **M11** Read-only Connections (probed health) and Settings (secrets masked to booleans).
- **M12** Control plane serves the built SPA at `/app` with security headers + CSP; frontend error boundary; route code-splitting.

## G12 — product gate

- [x] `make console` → dashboard → incident → root actor → evidence → causal path → export PDF → share, without reading the README.
- [x] SPA served in production by the control plane under `/app`, security headers + CSP applied.
- [x] RCA scoring/resolution algorithm untouched throughout (scope rule held).

### Explicitly deferred (not claimed as done)

Authentication / RBAC, CSRF protection, rate limiting, and list virtualization
are **not** implemented. The current security posture is: an optional shared
bearer token guards state-changing endpoints (`SRE_API_TOKEN`), read endpoints
are open in the local/demo deployment, secrets are never returned by the API,
and responses carry conservative security headers. Auth/RBAC is the natural next
epic before multi-tenant exposure.
