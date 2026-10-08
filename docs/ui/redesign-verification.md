# Investigative Graphite — implementation and verification

Implemented in the existing React 18 / TypeScript / Vite / Tailwind v4 console. Backend API DTOs, the deterministic RCA engine, report model, authentication handling, and notification reconciliation remain unchanged. Frontend types/client/hooks now also consume existing public revision and coverage APIs. No commits or pushes were made.

## Operator experience

- A shadcn sidebar-08-inspired inset shell with a 232px sticky sidebar and sidebar-07-inspired accessible icon collapse, Lucide product/navigation icons, active navigation treatment, existing unread counts, breadcrumbs, skip link, and a modal mobile navigation menu with theme control.
- Graphite/slate semantic tokens, locally hosted Inter and JetBrains Mono, compact 8px panels, restrained borders, reduced motion support, and a canvas up to 1800px. Existing saved theme preferences are honored; new visitors default to dark.
- The diagnosis panel separates impact, confidence, resolution, and claim level. Competing actors receive equal treatment and their own execution witnesses. Withheld actors stay withheld. Unknown execution endpoints are explicit, and full witness identity, UID, timestamps, and rule ID are available through disclosure. An execution witness suppresses the separate instance-resolution line, as required by D3.
- Causal paths display every source/relation/target hop independently, preserving resource kind, namespace, and the engine's direction field. Disjoint hops are never stitched into an invented chain. Empty paths make no causal assertion.
- Lifecycle summaries show timestamped alert occurrence, engine onset, incident recording, and stored diagnosis. Investigation phases and the original run/events are accessible through disclosure. Timestamp tooltips retain absolute values. Changes retain a separate, explicitly labeled onset window; temporal correlation does not become a causal claim.
- Findings are selectable structured rows. The Evidence Explorer searches resource identity, observation, kind, and evidence IDs; filters initiating/supporting/contradictory groups; and renders 30 observations at a time. Summary previews are bounded while all observations remain accessible.
- The right-side modal Evidence Inspector shows normalized observations, timing, full identity, explicit evidence IDs, matching raw provenance, and matching audit turns. Both joins use explicit evidence references. Absent raw rows are described honestly. The shadcn/Radix Sheet composition with Scroll Area provides modal focus containment, Escape handling, scroll locking, and explicit trigger focus restoration. Tabs use Radix roving focus and keyboard navigation.
- Investigation history is an expandable sequence of persisted read actions, exposing rationale, target/capability, authorization, execution, evidence novelty, affected hypotheses, resolution transitions, decision changes, gap/intent references, and stop reason. Original engine steps and assessed timing drivers remain accessible.
- Hypothesis cards retain full engine explanations and all reasons. Supported, unresolved, and contradicted candidates use informational treatment, with no conversion of scores into probability. Causal boundary relationships, frontier answers, uncertainty, and reference IDs are also accessible.
- Overview uses a compact operational counter strip and an active-incident/change canvas, beside a diagnosis activity stream and source health rail. Incidents retain server-side pagination/search and add existing status, confidence, and service filters with a clear action. Table incident links and keyboard row activation remain available.
- Changes provide contextual inspection with full change ID, source, revision, scope, timestamp, and onset-relative timing when present.
- Reports expose snapshot/run identity, library preview, exports, and email-sharing inspection. Preview and report-creation errors are visible; delivery status is rendered as returned by the API. Existing token handling and email idempotency/export semantics are unchanged.
- Connections distinguish a database probe, configured-but-unverified sources, receiver readiness, and reported connector capabilities. Settings renders configuration without presenting it as a verified health probe.
- Incidents, Changes, and Reports filters/search, incident workspace tabs, finding groups/search, and investigation filters persist in URL query parameters. Changing incident filters resets pagination atomically; clearing filters removes their parameters while preserving unrelated view state.
- Finding groups use counted, keyboard-accessible segmented tabs. Audit turns can be filtered by an explicit `decision_state_changed === true` or nonempty `new_evidence_refs`, without interpreting unknown decision changes. Explicit evidence-to-audit references open the requested turn and focus its expanded summary after the inspector closes; turn links survive reload.
- Copy controls preserve exact incident/evidence/resource/run/report/change identity and announce clipboard success or failure. Reports distinguish no stored snapshots from no search matches; a Radix dropdown groups the existing PDF/Markdown/JSON export URLs.
- Incident explorer links carry a validated local return destination, preserving filters and pagination through workspace reloads. Report search additionally matches exact or partial report, incident, and diagnosis-run identity.
- Audit references open a hypothesis inspector only when their exact IDs match the current diagnosis's hypothesis records. Missing hypothesis explanations remain explicitly unavailable; actors are never matched heuristically. Confidence/resolution badges provide keyboard- and touch-accessible explanations, distinguishing diagnosis resolution from incident recovery.
- The Timeline view combines explicit lifecycle, phase, event, and change timestamps. It labels occurrence versus recording time, groups equal instants without ordering them internally, separates unknown/invalid times, and renders at most 40 records before progressive disclosure. The original lifecycle details and change inspector remain accessible.
- A header bell opens a responsive notification center with a per-incident unread count, unread filter, individual/all read controls, incident links, and locally retained browser history (100 notices). Toast dismissal does not remove history. The existing first-visit silent baseline, 50-incident watch, SSE reconciliation, burst toast limits, and route-based read behavior remain unchanged. Menu opening alone does not mark notices read; timestamps are browser observation times, not invented backend occurrence times.

## Investigation workspace additions

- **Why not resolved?** surfaces the exact resolution rationale, unresolved dimensions, actor display, timing assessment, and frontier questions. Missing assessments stay missing. Question-to-hypothesis and question-to-finding links require explicit IDs.
- **Evidence navigation** adds previous/next finding controls within the selected explorer results, resource context, and causal explanation/frontier relationships joined only by recorded evidence IDs. Raw provenance and audit references retain independent empty/error states.
- **Resource Inspector** shows exact identity matches among incident findings, engine causal hops, and execution witnesses. It does not query current Kubernetes state or match resources by name alone.
- **Diagnosis history** uses existing core revision list/detail endpoints. It presents persisted metadata, full documents, cutoffs/digests, and the backend-computed difference against the linked predecessor: resolution, hypothesis states, appeared/disappeared hypotheses, eliminations, decisive references, and manifest status. First revisions and invalid revision deep links have explicit states.
- **Observation coverage** reads nullable coverage from the existing raw diagnosis endpoint and historical revision documents. It separates source continuity from transport completeness, retains gap timestamps and unknown starts, and never treats an absent record as healthy.
- **Recorded investigation** offers manual previous/next inspection of stored audit actions, persists the selected turn in the URL, and preserves rationale, authorization, returned/new/known references, and decision changes. This is not deterministic replay: no tools execute and no historical decision snapshot is reconstructed.
- **Operator action proposals** retain recorded action/risk/approval requirements and support command copying only. No approval or cluster execution workflow is introduced.
- Revision lists and latest coverage participate in the existing SSE query invalidation and polling fallback. Immutable revision details are not polled.
- These additions introduce no further dependencies or backend changes.

## File map

New components:

- `apps/web/src/components/RecentDiagnoses.tsx`
- `apps/web/src/components/ui/Input.tsx`, `ScrollArea.tsx`
- `apps/web/src/components/ui/FilterSelect.tsx`, `SegmentedControl.tsx`, `CopyButton.tsx`
- `apps/web/src/components/ReportExportMenu.tsx`, `apps/web/src/lib/useUrlFilters.ts`
- `apps/web/src/components/workspace/DiagnosisOverview.tsx`, `CausalBoundaries.tsx`
- `apps/web/src/components/Entity.tsx`
- `apps/web/src/components/workspace/DiagnosisPanel.tsx`
- `apps/web/src/components/workspace/EvidenceExplorer.tsx` (includes the evidence inspector)
- `apps/web/src/components/workspace/InvestigationHistory.tsx`, `InvestigationReplay.tsx` (UI: Recorded investigation)
- `apps/web/src/components/workspace/WhyNotResolved.tsx`, `ResourceInspector.tsx`, `RemediationProposal.tsx`
- `apps/web/src/components/workspace/DiagnosisHistory.tsx`, `EvidenceCoverage.tsx`
- `apps/web/src/api/types.ts`, `client.ts`, `hooks.ts`: existing core revision/coverage contracts
- `apps/web/src/components/workspace/TimingSection.tsx`
- `apps/web/src/components/workspace/HypothesisInspector.tsx`, `CorrelatedTimeline.tsx`, `apps/web/src/lib/incidentNavigation.ts`

Modified shell and shared components:

- `apps/web/src/index.css`, `src/app/Layout.tsx`, `src/lib/theme.ts`
- `src/components/PageHeader.tsx`, `StatTile.tsx`, `IncidentsTable.tsx`, `ChangesTable.tsx`, `SystemHealth.tsx`, `LiveBadge.tsx`
- `src/components/ui/Button.tsx`, `Card.tsx`, `Drawer.tsx`, `Table.tsx`, `Tabs.tsx`, `States.tsx`
- `src/components/workspace/CausalPath.tsx`, `CompetingHypotheses.tsx`, `FindingsList.tsx`, `LifecycleTimeline.tsx`, `RawEvidence.tsx`, `ReportExport.tsx`, `ShareReport.tsx`

Modified pages:

- `src/pages/IncidentWorkspacePage.tsx`, `OverviewPage.tsx`, `IncidentsPage.tsx`, `ChangesPage.tsx`, `ReportsPage.tsx`, `SettingsPage.tsx`
- Connections inherits its redesign through its existing shared components.

Infrastructure:

- `src/notifications/NotificationCenter.tsx`, `storage.test.ts`; expanded existing provider/context/diff/storage to retain observed notices independently of transient toasts.
- `apps/web/package.json`, `package-lock.json`, `.gitignore`
- `apps/web/playwright.config.ts`, `apps/web/e2e/console.spec.ts`, `investigation.spec.ts`

## Component selection

Compared shadcn sidebar-07 (icon collapse), sidebar-08 (inset content), sidebar-16 (global header), and dashboard-01 (operational table/header composition) through the official block registry. Selected the inset/collapse patterns without importing sample teams, users, charts, or decorative data. The default workspace now uses seven task views: Diagnosis, Evidence, Timeline & changes, Causal boundaries, Reports, Diagnosis history, and Investigation. Diagnosis and its exact causal hops form one primary panel; evidence sits immediately below; alternatives, lifecycle, and case context form a separate rail. This replaces the original long sequence of equal-weight cards.

[shadcn blocks](https://ui.shadcn.com/blocks), [Radix Sheet](https://ui.shadcn.com/docs/components/radix/sheet), [Radix Tabs](https://ui.shadcn.com/docs/components/radix/tabs), and [Tailwind v4 support](https://ui.shadcn.com/docs/tailwind-v4) were inspected. The Radix packages' published peer ranges explicitly include React 18. Local compositions preserve the console's public wrapper APIs and semantic tokens. Source attribution and the upstream MIT license are in `apps/web/THIRD_PARTY_NOTICES.md`.

Origin UI now redirects to coss UI. Its [input group](https://coss.com/ui/docs/components/input-group) and [toolbar](https://coss.com/ui/docs/components/toolbar) compositions informed compact search/filter styling, while Radix remains the single behavior foundation. No second Base UI component stack was introduced.

[React Flow UI](https://reactflow.dev/ui) and [TanStack Table](https://tanstack.com/table/latest) were evaluated. Current causal paths are short, read-only engine hop sequences; custom deterministic rows avoid graph layout/interaction overhead and preserve disjoint source identities. The backend has no sorting API and the existing tables already support bounded pagination; adding a table engine solely for visual styling was not justified.

## Dependencies

- `@radix-ui/react-popover`: accessible confidence/resolution explanations with Escape and focus restoration; React 18 peer compatibility checked. Definitions follow the console product contract and make no new causal claims.
- `@radix-ui/react-dropdown-menu`: accessible report export menu with keyboard navigation and trigger focus restoration. Published peer ranges include React 18; export endpoints and authentication semantics are unchanged.
- `@radix-ui/react-select`: shared shadcn-style filter controls for Incidents and Changes, with keyboard selection, focus restoration, scrollable menus, readable option labels, and responsive wider layouts. React 18 peer compatibility checked before installation. API filter values remain unchanged. Selected filters use semantic accent tokens; search fields occupy their own row. References: [shadcn Select](https://ui.shadcn.com/docs/components/radix/select), [coss Select](https://coss.com/ui/docs/components/select).
- `@radix-ui/react-dialog`, `@radix-ui/react-tabs`, `@radix-ui/react-scroll-area`: shadcn's accessible Sheet/Tabs/Scroll Area foundation; preserve existing Drawer/Tabs call sites.
- `class-variance-authority`: shadcn-style typed button variants adapted to the existing primary/secondary/ghost API.

- `lucide-react` 0.468.x: tree-shaken navigation and investigation icons. Its package peer range explicitly includes React 18; no Tailwind plugin is needed. [Official React documentation](https://lucide.dev/guide/react/).
- `@fontsource-variable/inter` and `@fontsource-variable/jetbrains-mono`: local font assets compatible with the production CSP, without third-party font requests. [Fontsource installation](https://fontsource.org/docs/getting-started/install).
- `@playwright/test` (development only): browser verification for focus behavior, domain edge cases, responsive layouts, filters, and report workflows. No graph library or large UI framework was added.
- The existing transitive `source-map-js` was updated to its patched compatible version. `npm audit` still flags two moderate findings associated with the existing React Router 6 dependency family. Its suggested fix is a major-version upgrade; that migration is outside this redesign. [Redirect advisory](https://github.com/advisories/GHSA-wrjc-x8rr-h8h6), [SSR hydration advisory](https://github.com/advisories/GHSA-337j-9hxr-rhxg).

## Verification results

All final checks passed:

```sh
npm --prefix apps/web run typecheck
npm --prefix apps/web run lint
npm --prefix apps/web run test                         # 13 tests passed
npm --prefix apps/web run build                        # production build passed
WEB_E2E_URL=http://localhost:8000/app npm --prefix apps/web run test:e2e
# 20 browser tests passed against production-served /app, including its CSP

.venv/bin/python -m pytest \
  tests/integration/test_console_api.py \
  tests/integration/test_console_email.py \
  tests/integration/test_console_stream.py \
  tests/unit/console tests/unit/report \
  tests/unit/rca/test_console_d3.py \
  tests/integration/test_diagnosis_revision_api.py \
  tests/integration/test_diagnosis_revisions.py \
  tests/unit/test_evidence_coverage.py -q
# 68 passed, 2 skipped (TEST_POSTGRES_URL unset); one existing Starlette/AnyIO deprecation warning

git diff --check                                     # passed
```

Browser matrix: 1920, 1440, 1100, 768, and 390px; both themes; all seven product routes (70 route/theme/width combinations), plus the new revision/evidence/recorded-investigation views (30 combinations). Ordinary document layouts passed overflow assertions; dense data tables retain contained horizontal scrolling. Other browser coverage includes long resource IDs, 95 evidence observations, disjoint causal hops, competing and withheld actors, verified-but-ambiguous diagnosis, unknown execution endpoints, exact/multiple instance states, stable/sensitive/unassessed timing, missing provenance, empty audits, audit disclosure fields, backward Tab containment, Escape/focus restoration, arrow-key tabs, inset sidebar collapse, explicit raw-provenance joins and long observations, server pagination parameters, actual report creation/preview, and unconfigured email refusal.

A computed-token contrast check found all nine tested text/surface and status/soft-background combinations at or above 4.5:1 in both themes (minimum 4.60:1 light, 5.56:1 dark). This is a targeted token check, not a full accessibility certification.

## Local preview and screenshots

The running preview uses a separate database, `.local/ui-redesign.db`, seeded by the repository's existing demo script. It is available at `http://localhost:8000/app`. The original local database was not modified.

To reproduce with a fresh local database:

```sh
mkdir -p .local
DATABASE_URL=sqlite:///.local/ui-redesign.db .venv/bin/python -m alembic upgrade head
DATABASE_URL=sqlite:///.local/ui-redesign.db .venv/bin/python scripts/seed_console_demo.py
npm --prefix apps/web run build
DATABASE_URL=sqlite:///.local/ui-redesign.db .venv/bin/agentic-sre serve
# In another terminal:
cd apps/web
npx playwright install chromium
WEB_E2E_URL=http://localhost:8000/app npm run test:e2e
```

The browser report-generation test writes a snapshot to the test/demo database. Run it against a disposable seeded database, not an operator deployment. Synthetic edge-state DTOs are confined to browser test interception and never appear as product/demo data.

Fourteen selected production screenshots are included in the repository's
[screenshot gallery](screenshots/README.md), with overview, incident explorer,
diagnosis, ambiguity, evidence, changes, reports, light theme, and mobile views.
The root README embeds the overview, workspace, and incident explorer.

Additional captures remain local, ignored artifacts in `.local/ui-redesign/`:

- `workspace-dark.png`, `workspace-light.png`, `workspace-mobile.png`
- `evidence-inspector.png`, `ambiguous-workspace-dark.png`
- `workspace-laptop.png`, `workspace-tablet.png`
- `overview-light-1920.png`, `evidence-view-dark.png`, `sidebar-collapsed.png`
- `incidents-dark.png`, `changes-dark.png`, `reports-dark.png`, `connections-dark.png`, `settings-dark.png`

Rendered captures were inspected for hierarchy, contrast, spacing, and overflow. The first pass led to a compact lifecycle summary and a fix for backward Tab wrapping. No live Kubernetes investigation was performed; actual browser data came from repository-provided seeds. Execution/timing/audit cases absent from those seeds were verified with explicitly synthetic test DTOs.

## Remaining boundaries

- Full deterministic replay, arbitrary revision-pair comparisons, a global coverage archive, and live resource topology remain outside the available console contracts. The recorded walkthrough and predecessor diff are explicitly scoped.
- The backend has no incident sorting or time-range filter parameters. These were not invented; its existing ordering/filter/pagination semantics remain authoritative.
- Provenance and relationships can only be displayed when explicit records and IDs exist. The UI does not fill missing capture history.
- The Evidence Explorer uses bounded incremental rendering rather than virtualization. Very large raw-provenance or hypothesis collections could benefit from further measured optimization.
- Browser verification used Chromium. Radix Sheet behavior on other supported browser engines should be included in a broader release matrix.
- Actual SMTP delivery and a live-cluster execution witness were not exercised in the browser; relevant backend contracts passed their regression tests.
