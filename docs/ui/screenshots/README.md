# Operator Console screenshots

These are browser captures of the actual frontend served from a production build,
using the repository's `scripts/seed_console_demo.py` data in a separate local
database. They are not mockups or live production incident records. Captured during
the Investigative Graphite redesign; relative ages and recorded timestamps reflect
that capture session.

## Operational overview

![Light-theme overview: active incidents, recent diagnoses, changes, and source health](overview-light.png)

## Incident explorer

![Dark-theme incident explorer with search, filters, and distinct severity, confidence, and resolution columns](incidents-dark.png)

## Incident workspace

![Dark-theme incident diagnosis with engine causal path, evidence basis, and recorded lifecycle](incident-workspace-dark.png)

## Ambiguous diagnosis

![Ambiguous incident workspace retaining engine uncertainty and alternative explanations](incident-ambiguous-dark.png)

## Incident chronology

![Combined incident timeline with explicit occurrence and recording times, equal-time groups, and changes](incident-timeline-dark.png)

## Evidence explorer

![Searchable normalized findings in the incident evidence explorer](evidence-explorer-dark.png)

## Evidence inspector

![Contextual evidence inspector with resource identity, timing, evidence IDs, and explicit missing-provenance state](evidence-inspector-dark.png)

## Resource inspector

![Resource inspector with exactly matched incident findings and recorded engine relationships](resource-inspector-dark.png)

## Diagnosis history

![Persisted diagnosis history with revision metadata and explicit first-revision state](diagnosis-history-dark.png)

The repository seed contains one revision for this incident, so no predecessor
comparison is invented. Multi-revision diffs, recorded actions, and coverage gaps
are verified separately with explicit browser-test fixtures.

## Changes

![Change explorer with resource kinds, scope, timestamps, and source](changes-dark.png)

## Reports

![Immutable report library with diagnosis identity, resolution, preview, exports, and sharing controls](reports-dark.png)

## Light-theme workspace

![Incident workspace in light theme](incident-workspace-light.png)

## Mobile workspace

<img src="incident-mobile.png" alt="Incident workspace at a 390-pixel mobile viewport" width="390" />

## Notification center

![Notification center with locally recorded incident notices, unread controls, and incident links](notification-center-dark.png)

This capture replays the repository-provided seeded incident list against an empty
remembered browser baseline to demonstrate the notification menu. Notice times are
browser observation times; the first visit normally establishes a silent baseline.

For implementation details, verification results, and capture limitations, see the
[redesign verification report](../redesign-verification.md).
