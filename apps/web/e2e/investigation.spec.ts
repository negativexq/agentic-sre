import { test, expect, type Page } from "@playwright/test";
import type {
  IncidentDetail,
  IncidentPage,
  DiagnosisRevisionDetail,
} from "../src/api/types";

async function fixture(page: Page) {
  const list = (await (
    await page.request.get("/api/v1/console/incidents?limit=100")
  ).json()) as IncidentPage;
  const id = list.items.find((item) => item.has_diagnosis)!.incident_id;
  return (await (
    await page.request.get(`/api/v1/console/incidents/${id}`)
  ).json()) as IncidentDetail;
}
async function open(page: Page, id: string, query = "") {
  await page.goto(
    `${process.env.WEB_E2E_URL ?? "http://localhost:5173"}/incidents/${id}${query}`,
  );
}

test("unresolved questions and evidence relationships use only explicit references; resource inspector preserves context", async ({
  page,
}) => {
  const data = await fixture(page);
  const d = data.diagnosis!;
  d.resolution = "INSUFFICIENT_EVIDENCE";
  d.leading_actor_display = "NOT_ESTABLISHED";
  d.leading_root_actor = null;
  d.root_cause = null;
  d.resolution_rationale = "Recorded distinction has not been established.";
  const finding = d.evidence[0];
  d.frontier_answers = [
    {
      alternative_id: "unmatched-alternative",
      question: "Recorded test question?",
      state: "UNRESOLVED",
      investigation_state: "NOT_ESTABLISHED",
      evidence_ids: finding.evidence_ids,
      remaining_uncertainty: ["Recorded remaining uncertainty."],
    },
  ];
  d.causal_explanations = [
    {
      explaining_claim: "claim-a",
      explained_claim: "claim-b",
      mechanism: "Explicit linked mechanism",
      consequence: "Explicit consequence",
      evidence_ids: finding.evidence_ids,
    },
    {
      explaining_claim: "claim-other",
      explained_claim: "claim-none",
      mechanism: "Unrelated mechanism must stay absent",
      consequence: "",
      evidence_ids: ["unmatched-id"],
    },
  ];
  await page.route(`**/api/v1/console/incidents/${d.incident_id}`, (route) =>
    route.fulfill({ json: data }),
  );
  await open(page, d.incident_id);
  await expect(
    page.getByText("Why not resolved?", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Recorded test question?", { exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: /^Evidence/ }).click();
  await page
    .getByRole("button", { name: /^Inspect finding:/ })
    .first()
    .click();
  const inspector = page.getByRole("dialog", { name: "Evidence inspector" });
  await expect(
    inspector.getByText("Explicit linked mechanism", { exact: true }),
  ).toBeVisible();
  await expect(
    inspector.getByText("Unrelated mechanism must stay absent"),
  ).toHaveCount(0);
  if (d.evidence.length > 1) {
    await inspector.getByRole("button", { name: "Next finding" }).click();
    await expect(
      inspector.getByText(`Finding 2 of ${d.evidence.length}`, { exact: true }),
    ).toBeVisible();
    await inspector.getByRole("button", { name: "Previous finding" }).click();
  }
  await inspector
    .getByRole("button", { name: "Open resource context" })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Resource inspector" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

function revision(number: number): DiagnosisRevisionDetail {
  return {
    diagnosis_id: number,
    revision_number: number,
    previous_diagnosis_id: number === 1 ? null : 1,
    trigger: "TEST_REVISION",
    created_at: "2026-10-09T10:00:00Z",
    run_id: `run-${number}`,
    window_end: null,
    manifest_digest: null,
    tape_digest: null,
    epistemic_digest: null,
    engine_version: null,
    config_digest: null,
    root_cause: null,
    confidence: "UNVERIFIED",
    mode: "test",
    resolution: number === 1 ? "INSUFFICIENT_EVIDENCE" : "AMBIGUOUS",
    diagnosis: { summary: `Stored summary ${number}`, evidence_coverage: null },
    diff:
      number === 1
        ? null
        : {
            resolution_transition: {
              previous: "INSUFFICIENT_EVIDENCE",
              current: "AMBIGUOUS",
              changed: true,
            },
            hypothesis_changes: [],
            new_eliminations: [],
            new_decisive_evidence_ids: ["recorded-decisive-evidence"],
            appeared: [],
            disappeared: [],
            manifest_diff: {
              previous_digest: null,
              current_digest: null,
              status: "UNKNOWN",
            },
          },
  };
}

test("diagnosis history renders backend predecessor diff, first revision and invalid deep links", async ({
  page,
}) => {
  const data = await fixture(page);
  const id = data.incident.incident_id;
  await page.route(`**/api/v1/incidents/${id}/diagnoses`, (route) =>
    route.fulfill({ json: [revision(1), revision(2)] }),
  );
  await page.route(`**/api/v1/incidents/${id}/diagnoses/*`, (route) =>
    route.fulfill({
      json: revision(Number(route.request().url().split("/").at(-1))),
    }),
  );
  await open(page, id, "?tab=revisions");
  await expect(
    page.getByText("Stored summary 2", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Resolution changed", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("recorded-decisive-evidence", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: /^Revision 1/ }).click();
  await expect(page).toHaveURL(/revision=1/);
  await expect(
    page.getByText("No predecessor diff recorded for this revision."),
  ).toBeVisible();
  await open(page, id, "?tab=revisions&revision=999");
  await expect(
    page.getByText("The requested revision is not recorded for this incident."),
  ).toBeVisible();
});

test("coverage distinguishes recorded gaps, delivery and unknown scope; absence is not healthy", async ({
  page,
}) => {
  const data = await fixture(page);
  const id = data.incident.incident_id;
  await page.route(`**/api/v1/incidents/${id}/diagnosis`, (route) =>
    route.fulfill({
      json: {
        evidence_coverage: {
          starts_at: "2026-10-09T09:00:00Z",
          window_end: "2026-10-09T10:00:00Z",
          stream_followed_since: null,
          scopes: [
            {
              namespace: "production",
              kind: "Pod",
              source_continuity: "GAPPED",
              transport_completeness: "PROVEN",
              transport_proven_at: null,
              gaps: [
                {
                  reason: "Recorded disconnect",
                  since: null,
                  at: "2026-10-09T09:20:00Z",
                  namespace: "production",
                  kind: "Pod",
                },
              ],
            },
            {
              namespace: "",
              kind: "Service",
              source_continuity: "UNKNOWN",
              transport_completeness: "NOT_APPLICABLE",
              transport_proven_at: null,
              gaps: [],
            },
          ],
        },
      },
    }),
  );
  await open(page, id, "?tab=evidence");
  await expect(page.getByText("GAPPED", { exact: true })).toBeVisible();
  await expect(page.getByText("NOT_APPLICABLE", { exact: true })).toBeVisible();
  await page.getByText("Recorded gaps (1)", { exact: true }).click();
  await expect(
    page.getByText("Recorded disconnect", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/Since unknown start/)).toBeVisible();
  await page.unroute(`**/api/v1/incidents/${id}/diagnosis`);
  await page.route(`**/api/v1/incidents/${id}/diagnosis`, (route) =>
    route.fulfill({ json: { evidence_coverage: null } }),
  );
  await page.reload();
  await expect(
    page.getByText(/No evidence coverage record was stored for this diagnosis/),
  ).toBeVisible();
});

test("recorded investigation walks persisted actions without executing tools and restores selected turn from URL", async ({
  page,
}) => {
  const data = await fixture(page);
  const d = data.diagnosis!;
  const action = {
    turn_index: 4,
    gap_id: null,
    gap_dimension: null,
    missing_fact: null,
    intent_id: null,
    intent_kind: null,
    action: "recorded-read",
    capability: "kubernetes",
    target: "production/Pod/recorded",
    action_rationale: "Recorded first rationale",
    authorization_result: "ALLOWED",
    authorization_reason: "Recorded allowed reason",
    backend_execution_status: "SUCCEEDED",
    observation_id: "observation-4",
    observation_outcome: "RETURNED",
    returned_evidence_refs: ["ref-4"],
    new_evidence_refs: ["ref-4"],
    already_known_refs: [],
    normalized_finding_ids: [],
    affected_hypothesis_ids: [],
    resolution_before: "INSUFFICIENT_EVIDENCE",
    resolution_after: "AMBIGUOUS",
    decision_state_changed: true,
    progress_classification: "NEW_EVIDENCE",
  };
  d.investigation_audit = {
    diagnosis_run_id: "recorded-run",
    artifact_version: "test",
    initial_resolution: "INSUFFICIENT_EVIDENCE",
    final_resolution: "AMBIGUOUS",
    stop_reason: "RECORDED_STOP",
    turns: 2,
    model_calls: 0,
    tool_calls: 2,
    action_audits: [
      action,
      {
        ...action,
        turn_index: 8,
        action_rationale: "Recorded second rationale",
        new_evidence_refs: [],
        already_known_refs: ["ref-4"],
        decision_state_changed: false,
      },
    ],
  };
  await page.route(`**/api/v1/console/incidents/${d.incident_id}`, (route) =>
    route.fulfill({ json: data }),
  );
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") writes.push(request.url());
  });
  await open(page, d.incident_id, "?tab=trace&investigation_view=recorded");
  await expect(
    page.getByText("Recorded first rationale", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Next action", exact: true }).click();
  await expect(
    page.getByText("Recorded action 2 of 2 · Turn 8", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Next action", exact: true }),
  ).toBeDisabled();
  await page.reload();
  await expect(
    page.getByText("Recorded second rationale", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Previous action", exact: true })
    .click();
  await expect(
    page.getByText("Recorded first rationale", { exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([]);
});

test("new investigation views fit desktop, laptop and mobile in both themes; empty history remains explicit", async ({
  page,
}) => {
  const data = await fixture(page);
  const id = data.incident.incident_id;
  await open(page, id);
  for (const theme of ["light", "dark"]) {
    await page.evaluate(
      (value) => localStorage.setItem("agentic-sre-theme", value),
      theme,
    );
    for (const width of [1920, 1440, 1100, 768, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const query of [
        "?tab=revisions",
        "?tab=evidence",
        "?tab=trace&investigation_view=recorded",
      ]) {
        await open(page, id, query);
        await expect(
          page.getByRole("tab", { name: /^Investigation/ }),
        ).toBeVisible();
        if (query.includes("revisions"))
          await expect(
            page.getByText("Persisted diagnosis comparison", { exact: true }),
          ).toBeVisible();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBeTruthy();
      }
    }
  }
  data.diagnosis!.investigation_audit = null;
  await page.route(`**/api/v1/console/incidents/${id}`, (route) =>
    route.fulfill({ json: data }),
  );
  await open(page, id, "?tab=trace&investigation_view=recorded");
  await expect(
    page.getByText(
      /No recorded investigation actions are available for this walkthrough/,
    ),
  ).toBeVisible();
});
