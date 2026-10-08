# API support for additional operator pages

Inspected the console router/DTOs, core control-plane routes, revision schemas,
storage repositories, and frontend API client. Read-only runtime checks against
the local seeded server confirmed service filtering, revision listing, and
revision detail return HTTP 200. No backend contract or RCA code was changed.

| Page | Existing support | Boundary |
| --- | --- | --- |
| Diagnosis History | `GET /api/v1/incidents/{id}/diagnoses` and `/diagnoses/{revision_number}` | Implemented in the incident workspace using typed client/hooks and backend predecessor diff. The seeded incident checked has one revision and no predecessor diff. |
| Service Investigation | `/api/v1/console/incidents?service=...&limit=...&offset=...`, incident detail, evidence, reports | Service filtering matches only the list item's first diagnosis service. No service catalog, complete service/resource mapping, or exact service change filter exists. |
| Evidence Coverage | Raw `/api/v1/incidents/{id}/diagnosis` and revision detail include nullable `evidence_coverage` | Incident/revision-level coverage is available when recorded. Console `DiagnosisView` omits it. The checked seed has null coverage; no global coverage/gap endpoint exists. |
| Investigation Runs | Incident detail contains the latest run's `investigation_audit` | No global run list or historical audit-by-run HTTP endpoint. Loading individual incidents is possible but not a scalable global archive. |
| Incident Comparison | Two console incident detail responses provide current diagnoses, findings, alternatives, paths, and audits | A read-only side-by-side comparison can use the existing API. Similarity is not proof of a shared cause; a revision's own diff is only against its persisted predecessor. |

Revision summary fields include revision number, predecessor diagnosis ID, trigger,
creation time, run ID, observation cutoff, engine/config identity, manifest/tape/
epistemic digests, actor, confidence, and resolution. Detail returns the stored
diagnosis and a nullable backend-computed predecessor diff: resolution transition,
hypothesis changes, appeared/disappeared hypotheses, new eliminations, decisive
evidence IDs, and manifest status. The UI need not compute new causal conclusions.

Coverage records contain observation window, stream-following start, and scopes
with namespace/kind, source continuity, gaps, transport completeness, and transport
proof time. Null coverage must be shown as unrecorded, never as healthy coverage.

Global change search supports resource-name query, scope, type, and a bounded limit
(maximum 500), but no service parameter or pagination. Incident changes use an
onset window of **−2 hours / +30 minutes**, capped at 200 records. The previous
`±2h` UI/documentation label was corrected during this audit.

Diagnosis History and incident/revision coverage are now implemented in the workspace. A future Service Investigation view can use explicit incident data. A complete service catalog, global coverage view, and global
run archive require additional read-only backend projections.

Source files: `apps/control_plane/main.py`, `apps/control_plane/schemas.py`,
`apps/control_plane/console/router.py`, `dto.py`, `mappers.py`,
`packages/storage/repositories.py`, `packages/rca/evidence_coverage.py` (read only),
`apps/web/src/api/client.ts`, and `types.ts`.
