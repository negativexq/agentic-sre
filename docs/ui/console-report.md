# Agentic SRE Operator Console — Build Report

A full account of the UI/productization track (**M0–M12**): what was built, how
it is structured, how it was verified, and what was deliberately left out. The
console is built strictly on top of the deterministic RCA engine — it renders
the engine's output and never computes a causal claim of its own. The UI track
did not modify the RCA scoring/resolution implementation (`packages/rca/` is
unchanged across the track). The latest **recorded** live-suite result remains
24/25 with 0 confident fabrications and 0 model calls; it was **not re-measured**
as part of this UI track (control-plane and storage code did change).

- **Scope:** 13 milestones (M0–M12), delivered as **14 milestone commits** (M2
  landed in two) plus follow-up fixes, on `main`.
- **Verification:** `make check` green — ruff + mypy (strict) over `apps packages
  tests scripts` + **783 backend/console tests**; frontend `tsc -b` + `eslint` +
  production build. No automated React/component/browser tests yet.
- **Live demo:** `make console` → http://localhost:8000/app (no cluster needed).

---

## 1. Product shape

Six product areas across **seven routes** (the incident list and the incident
workspace are separate routes), one design system, one honest data contract.

| Screen | Route | What it answers |
|---|---|---|
| Overview | `/` | What is happening across the cluster right now? |
| Incidents | `/incidents` | Every incident, filterable by impact and outcome. |
| Incident workspace | `/incidents/:id` | What broke, who is the root actor, why, what alternatives remain. |
| Changes | `/changes` | Every recorded change fact; onset-relative inside an incident. |
| Reports | `/reports` | Immutable report snapshots, exportable and shareable. |
| Connections / Settings | `/connections`, `/settings` | Connector health and read-only config. |

### Terminology (single-meaning, frozen in M0)

- **Root cause** — the engine's selected actor once a diagnosis is `RESOLVED`.
- **Leading root actor** — the top-ranked actor when unresolved; alternatives remain.
- **Confidence** — `VERIFIED` / `LIKELY` / `UNVERIFIED`.
- **Resolution** — `RESOLVED` / `AMBIGUOUS` / `INSUFFICIENT_EVIDENCE`.

Two invariants the UI enforces visually:

- **`AMBIGUOUS ≠ WRONG`.** Ambiguous/insufficient states render **neutral**,
  never as an error color. The workspace shows an explicit "Ambiguous is not
  wrong" banner explaining the engine has a leading actor but could not
  positively exclude a structural alternative.
- **The UI authors no causal claim.** Root actor, confidence, resolution, causal
  path, competing hypotheses, and "leading actor" change marks all come verbatim
  from the stored diagnosis.

Full contract: [product-contract.md](product-contract.md).

---

## 2. Architecture

```
Browser (SPA, /app)
   │  fetch /api/v1/console/*   +   EventSource /api/v1/console/**/stream
   ▼
FastAPI control plane
   ├─ /api/v1/console/*  ── console router (DTOs)  ── mappers ── repositories ── Postgres/SQLite
   ├─ /app               ── serves apps/web/dist (SPA) + security headers/CSP
   ├─ /api/v1/*          ── existing raw-contract API (unchanged)
   └─ /                  ── existing server-rendered pages (unchanged)
```

Key boundary decisions:

- **Dedicated DTO surface.** The console talks only to `/api/v1/console/*`, whose
  Pydantic DTOs (`apps/control_plane/console/dto.py`) are the stable wire
  contract. The frontend never sees internal persistence rows or the full RCA
  model. The pre-existing `/api/v1/*` endpoints and their tests are untouched.
- **Bounded queries, no N+1.** Incident list/data assembly is two queries —
  incidents (1) + `DiagnosisRepository.latest_views()` (1); service, resolution,
  confidence and root actor come from the latest stored diagnosis document, not
  per-row fetches. The dashboard additionally runs a `SELECT 1` health probe, so
  its total is three bounded queries — still no N+1.
- **Timeline binding by id.** The lifecycle timeline is bound to a diagnosis by
  its `diagnosis_run_id` (the v2a invariant), shared between the server-rendered
  page and the console via `apps/control_plane/timeline.py`. A run whose
  completing event was lost shows "Timeline unavailable" rather than borrowing
  another run's phases.

---

## 3. API surface — `/api/v1/console/*` (21 endpoints)

State-changing endpoints (`POST /incidents/{id}/reports`,
`POST /reports/{id}/email`) require the shared bearer token when `SRE_API_TOKEN`
is set; every GET is read-only and open, matching the legacy API.

**Overview / config**
- `GET /dashboard` → counters + active incidents + recent diagnoses + system health
- `GET /system` → connector health (probed where possible)
- `GET /settings` → read-only effective config, secrets masked to booleans

**Incidents**
- `GET /incidents` → filtered + paginated list (`status, severity, service, confidence, resolution, active, q, limit, offset`)
- `GET /incidents/{id}` → composite detail (header + diagnosis view + timeline)
- `GET /incidents/{id}/diagnosis` → the diagnosis view alone (404 if none)
- `GET /incidents/{id}/timeline` → alert→diagnosis lifecycle, run-bound
- `GET /incidents/{id}/evidence` → raw provenance rows
- `GET /incidents/{id}/changes` → changes in ±2h onset window, leading-actor marked

**Changes**
- `GET /changes` → global change explorer (`scope, change_type, q, limit`)

**Reports**
- `POST /incidents/{id}/reports` → freeze an immutable snapshot (201; 409 if no diagnosis)
- `GET /reports` · `GET /incidents/{id}/reports` → library / per-incident
- `GET /reports/{id}` → the snapshot (JSON)
- `GET /reports/{id}/markdown` · `/json` · `/pdf` → exports from the same snapshot

**Sharing** (token-guarded when configured)
- `POST /reports/{id}/email` → share (401 without token, 503 if unconfigured, 422 no recipient, 502 on send failure; atomically idempotent by key)
- `GET /reports/{id}/deliveries` → audit log

**Live**
- `GET /stream` → global SSE (incidents/events/diagnoses changed)
- `GET /incidents/{id}/stream` → per-incident SSE

---

## 4. Milestone log

### M0 — Product contract
Froze IA, terminology, screen contracts, color semantics, and the scope rule.
No code; `docs/ui/product-contract.md`.

### M1 — Frontend foundation
`apps/web/` Vite + React 18 + TypeScript + Tailwind v4 + TanStack Query + React
Router. Semantic design tokens as CSS variables with class-based dark mode.
Primitives: Button, Badge, Card, Table, Tabs, Drawer, Timeline, CodeBlock,
Skeleton/EmptyState/Alert. Responsive shell with sidebar + mobile top nav.

### M2 — API/UI contract
Console DTOs + mappers + router mounted at `/api/v1/console`. Typed TS client
(`api/client.ts`), wire types (`api/types.ts`), and React Query hooks
(`api/hooks.ts`). Repository addition: `DiagnosisRepository.latest_views()`.
Latent read-path fix: `EvidenceRepository` now coerces stored ISO time windows.

### M3 — Overview dashboard
Counters (active / critical / diagnosing / median diagnosis time), active-incident
table, recent diagnoses, system health. Demo seeder `scripts/seed_console_demo.py`.

### M4 — Incident workspace
The product's core screen: header, leading-root-actor card (with the
"ambiguous is not wrong" banner), causal path (engine hops), run-bound lifecycle
timeline, two-column "why this actor" vs "competing hypotheses", and an
evidence/trace tab set with lazy raw-provenance drill-down.

### M5 — Live updates (SSE)
`apps/control_plane/console/stream.py` streams **only persisted-state
transitions**: the server polls a cheap fingerprint of `(incidents, events,
diagnoses)` counts and emits an SSE event on change. The client
(`api/useLiveUpdates.ts`) refetches the authoritative DTO on each signal, so the
store stays the source of truth; EventSource reconnects on its own and
reconciles. Proven end-to-end: a live DB insert moved the fingerprint
`[5,21,4] → [6,22,4]` within ~1s with no client action. Failed diagnosis runs
surface a distinct state.

### M6 — Changes explorer
Global change timeline + onset-relative incident view. Changes touching the
engine's leading actor are marked with a neutral "leading actor" badge — a label,
not a UI causal claim. Repository addition: `ChangeRecordRepository.recent()`.

### M7 — Canonical report
`packages/report/` — an immutable `ReportSnapshot` pinned to `diagnosis_run_id`,
built deterministically by `build_report()` from the stored diagnosis + recorded
timing. Persisted via `ReportRepository` (table `report_snapshots`, migration
`0012`). Re-diagnosing an incident never mutates an existing report; a new report
is a new row.

### M8 — Export (PDF / Markdown / JSON)
`packages/report/render.py`: `to_markdown()` (executive summary + technical RCA)
and `to_pdf()` (real PDF via **fpdf2**, no system libraries). Every export reads
only the snapshot, so all three formats agree on root actor / confidence /
resolution. In-app preview renders the Markdown with a small purpose-built
renderer (`components/Markdown.tsx`) in a Drawer.

### M9 — Reports library
`/reports` lists immutable snapshots (multiple per incident = history), each with
inline PDF/MD/JSON export and a link back to its incident and run.

### M10 — Email sharing
`packages/report/email.py` renders the email from the same snapshot;
`apps/control_plane/console/email_delivery.py` sends over SMTP when
`SRE_SMTP_HOST` is configured. Each share **reserves** a `pending` delivery row
before any send (the `idempotency_key` unique constraint is the concurrency
gate: of two racing requests with the same key, exactly one insert wins and
sends; the loser returns the existing row without sending). The row is then
finalized to `sent`/`failed`; failures are recorded and surfaced (502) for
retry. Unconfigured is an **honest refusal** (503), and when a token is
configured the endpoint requires it (401 otherwise). The UI shows a "set
`SRE_SMTP_HOST`" note with the send control disabled — nothing pretends to send.

### M11 — Connections & Settings
Read-only. Connections shows connector health: **Database** is a real `SELECT 1`
probe; **Kubernetes** reflects only whether a cluster reader is configured;
**Alertmanager** is reported "connected — webhook receiver ready" (our receiver
is up; upstream Alertmanager reachability is not probed); Prometheus/Loki/Tempo/
Email are reported configured-or-not from the environment. Settings shows
effective config; **secrets are never returned** — only whether each is set.

### M12 — Production hardening
The control plane serves the built SPA under `/app` (deep client routes fall back
to the shell; assets under `/app/assets`), with conservative security headers and
a strict Content-Security-Policy on every response. Frontend gains a top-level
error boundary and route-level code-splitting (main bundle 297 → 247 kB, pages
lazy-loaded). Makefile: `make console` (build + seed + serve), `web-build`,
`web-dev`.

---

## 5. Data model additions

| Table | Migration | Purpose |
|---|---|---|
| `report_snapshots` | `0012` | Immutable report, pinned to `diagnosis_run_id`. |
| `email_deliveries` | `0013` | Audit log of shares (recipients, status, error, idempotency key). |

Migrations are linear (`… → 0011 → 0012 → 0013`) and idempotent. Additive schema
changes only — **two new tables**, no mutation of existing RCA/diagnosis tables.

---

## 6. Verification

- **Backend/console:** 783 tests pass; ruff + mypy (strict) clean over `apps
  packages tests scripts`. These are Python tests of the console/report backend —
  **not** React UI tests.
- **Dedicated console/report tests (36):**
  - `test_console_api.py` (16) — dashboard counters, list filters + pagination,
    detail timeline binding, `AMBIGUOUS` not resolved, timeline-unavailable,
    evidence, changes + leading-actor mark, report create/fetch/immutability,
    report-create token guard, exports, settings masking, legacy API unchanged,
    typed 404s.
  - `test_console_stream.py` (2) — fingerprint tracking; SSE pump emits
    retry + initial + on-change.
  - `test_console_email.py` (6) — unconfigured refusal, send + audit,
    idempotency, token guard, recipient validation, failed-delivery recording.
  - `test_change_mapper.py` (3) — leading-actor mark by kind+name, no
    false-positive on same-name/different-kind, no mark without a leading actor.
  - `test_builder.py` / `test_render.py` / `test_email.py` (9) — snapshot
    projection, resolved vs ambiguous, determinism, Markdown/PDF/email content.
  - `test_api.py` also covers security headers + SPA serving.
- **Frontend:** TypeScript typecheck, ESLint, and production build pass; every
  screen verified manually in the browser against real seeded data, including the
  **production-served** SPA at `:8000/app` under CSP. Automated
  React/component/browser tests are **not** implemented (no `test` script in
  `apps/web/package.json`).

---

## 7. Honest scope — not implemented

Claimed nowhere as done:

- **User authentication / RBAC**, CSRF protection, rate limiting, list virtualization.

Current security posture: an optional shared bearer token (`SRE_API_TOKEN`)
guards **all** state-changing endpoints — the legacy write endpoints and the
console's `POST /incidents/{id}/reports` and `POST /reports/{id}/email` (verified
by tests). Read endpoints are open in the local/demo deployment; secrets are
never returned by the API; responses carry `X-Content-Type-Options`,
`X-Frame-Options`, `Referrer-Policy`, and a strict CSP. This is a single shared
secret, not per-user auth: **user authentication and RBAC** are the natural next
epic before any multi-tenant exposure.

Also note: the raw `evidence` table has no writer in the current pipeline, so the
"Raw provenance" tab is typically empty in the live product — the engine reasons
over findings (shown in the "Evidence" tab), and raw rows appear only when
captured.

---

## 8. File map

```
apps/web/src/
  api/            client.ts, types.ts, hooks.ts, useLiveUpdates.ts
  app/            Layout.tsx, router.tsx, ErrorBoundary.tsx
  components/     StatTile, IncidentsTable, ChangesTable, SystemHealth,
                  LiveBadge, PageHeader, Markdown
    ui/           Button, Badge, Card, Table, Tabs, Drawer, Timeline,
                  CodeBlock, States
    workspace/    CausalPath, FindingsList, CompetingHypotheses,
                  LifecycleTimeline, RawEvidence, ReportExport, ShareReport
  lib/            cn, format, tones, theme, queryClient
  pages/          Overview, Incidents, IncidentWorkspace, Changes, Reports,
                  Connections, Settings, NotFound

apps/control_plane/
  auth.py         shared bearer-token guard (legacy + console writes)
  console/
    dto.py          UI wire contracts
    mappers.py      domain → DTO
    router.py       the 21 endpoints
    stream.py       SSE fingerprint pump
    settings.py     read-only config view (secrets masked)
    email_delivery.py  SMTP delivery + env config

packages/report/  model.py, builder.py, render.py (PDF/MD), email.py
scripts/seed_console_demo.py   demo data seeder
```

---

## 9. Running it

```bash
make console          # build SPA + migrate + seed + serve
# → http://localhost:8000/app

make web-dev          # Vite dev server (hot reload) against a running control plane
make ui               # against a live cluster: port-forward → :8080/app
```
