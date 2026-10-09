# Shared incident investigation canvas

The workspace now has one URL-backed investigation selection shared across task
views. There are no additional primary navigation pages.

## Interaction contract

- Timeline changes and normalized observations select the common canvas and
  reveal it with keyboard focus. Occurrence/recording semantics and unknown-time
  separation remain unchanged. Selecting a change does not mark it causal.
- Causal X-Ray deduplicates only identical resource identities and draws exactly
  the engine's supplied hops. Disconnected components remain separate; parallel
  edges, cycles, and self edges are retained. Numbered edges correspond to the
  relationship controls, which expose original source, target, relation and
  direction. Edge-level provenance is explicitly unavailable in this DTO.
- Selecting a graph node opens the first exactly matched finding in the Evidence
  Inspector, with the remaining matching findings available through navigation.
  Nodes without matching findings select resource context and show the absence
  honestly. The original hop ledger remains available through disclosure.
- The graph is shown in Diagnosis until the shared canvas is expanded. The canvas
  then keeps it visible across Evidence, Timeline, Investigation and other task
  views, alongside linked findings and current hypothesis references.
- Evidence Explorer scopes results to selected resource/change/action references.
  Operators can show all findings while preserving the highlight. Direct finding
  selection highlights its identity without narrowing the list. Inspecting a
  finding within a selected resource/action keeps that investigation scope.
- Recorded investigation previous/next controls change the common selection, not
  just the audit table. The canvas also provides recorded-turn controls that work
  across task views. Evidence joins use returned evidence IDs; hypotheses use
  affected hypothesis IDs; targets are treated as resources only when their exact
  identities appear in current incident records. Unmatched IDs remain visible.
- The current diagnosis, hypothesis states and causal graph are never replaced
  with invented historical states. The canvas explicitly labels them as current
  stored context, and recorded-action resolution transitions remain audit facts.
  No read tools, remediation or replay engine are executed.
- Clear selection restores the complete incident evidence list. Selection,
  collapse state, recorded step and explorer preferences survive URL reloads.

## Namespace projection

`ChangeView` now has one additive, nullable `namespace` field. It is projected
only from explicit `before`/`after` snapshot `metadata.namespace`, with matching
resource name/kind where supplied. Conflicting metadata, missing metadata or
invalid values produce null. No storage or ChangeRecord contract changes were
needed; the RCA engine and legacy `matches_leading_actor` behavior are unchanged.

This field is needed to join a timeline change to `namespace/kind/name` safely.
Kind/name alone, timing proximity, and the existing leading-actor marker are not
used as identity proof. The repository's older seeds have no retained namespace
in these snapshots; their change selections explicitly explain that no automatic
resource/evidence join is possible. Normalized observations can still be selected
by their complete recorded identities.

## Implementation

- `apps/web/src/components/investigation/selection.ts`: pure reference resolution.
- `context.ts`, `InvestigationCanvas.tsx`: shared URL-backed context and canvas.
- `graph.ts`, `CausalXRay.tsx`: deterministic read-only node/edge presentation.
- `IncidentWorkspacePage.tsx`: provider, selection and inspector coordination.
- `CausalPath.tsx`, `DiagnosisOverview.tsx`, `CorrelatedTimeline.tsx`,
  `EvidenceExplorer.tsx`, `FindingsList.tsx`, `CompetingHypotheses.tsx`,
  `InvestigationReplay.tsx`, `InvestigationHistory.tsx`, `ChangesTable.tsx`:
  participating views.
- `Drawer.tsx`, `ResourceInspector.tsx`: equivalent graph-trigger focus
  restoration after the graph moves into the persistent canvas.
- `apps/control_plane/console/dto.py`, `mappers.py`: namespace projection only.

No dependency was added. A small custom graph retains accessible HTML resource
buttons and bounded graph scrolling without React Flow layout/interaction cost.
Layout is memoized; selection updates do not recompute graph positions. Dense
horizontal scrolling is contained inside the explicitly labeled graph region.

## Verification

See [the complete verification report](redesign-verification.md) for commands.
Pure tests cover namespace collisions, missing identities, admitted-reference
boundaries, unknown selections, disconnected graphs, parallel edges and cycles.
Browser tests cover cross-tab selection, URL reload, graph-to-evidence navigation,
keyboard focus restoration, recorded-turn coordination, and both themes at
1920/1440/1100/768/390px. Backend regressions cover optional namespace metadata,
conflicting metadata and existing console/report/SSE/revision behavior.

The public screenshot gallery uses actual repository seed data. Explicit synthetic
edge cases are confined to browser test interception and ignored local captures.
