import { test, expect, type Page } from "@playwright/test";
import type { IncidentDetail, IncidentPage } from "../src/api/types";

async function incident(page: Page) {
  const response = await page.request.get(
    "/api/v1/console/incidents?limit=100",
  );
  expect(response.ok()).toBeTruthy();
  const list = (await response.json()) as IncidentPage;
  const item = list.items.find((item) => item.has_diagnosis);
  if (!item)
    throw new Error(
      "Seed the control plane with scripts/seed_console_demo.py before running browser tests.",
    );
  return (await (
    await page.request.get(`/api/v1/console/incidents/${item.incident_id}`)
  ).json()) as IncidentDetail;
}
async function open(page: Page, path: string) {
  await page.goto(
    `${process.env.WEB_E2E_URL ?? "http://localhost:5173"}${path}`,
  );
}
async function noPageOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
}

test("seeded workspace: evidence inspector traps and restores focus; tabs use arrow navigation", async ({
  page,
}) => {
  const data = await incident(page);
  await open(page, `/incidents/${data.incident.incident_id}`);
  await expect(
    page.getByText("Deterministic diagnosis", { exact: true }),
  ).toBeVisible();
  const finding = page
    .getByRole("button", { name: /^Inspect finding:/ })
    .first();
  await finding.click();
  await expect(
    page.getByRole("dialog", { name: "Evidence inspector" }),
  ).toBeVisible();
  await page.keyboard.press("Shift+Tab");
  expect(
    await page.evaluate(() =>
      Boolean(document.activeElement?.closest("[role=dialog]")),
    ),
  ).toBeTruthy();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(finding).toBeFocused();
  await page.getByRole("tab", { name: /^Evidence/ }).focus();
  await page.keyboard.press("End");
  await expect(
    page.getByRole("tab", { name: /^Investigation/ }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByText(
      "Recorded reads, their authorization, and their effect on the diagnosis.",
    ),
  ).toBeVisible();
});

test("engine edge states: competing actors, unknown execution time, absent provenance, bounded large collections", async ({
  page,
}) => {
  const data = await incident(page);
  const d = data.diagnosis!;
  d.leading_actor_display = "COMPETING";
  d.resolution = "AMBIGUOUS";
  d.is_resolved = false;
  d.leading_actor_candidates = [
    "production/NetworkChaos/network-latency-" +
      "very-long-resource-name-".repeat(10),
    "production/Deployment/payments",
  ];
  d.executing_instances = [
    {
      actor: d.leading_actor_candidates[0],
      instance: "production/NetworkChaos/latency-instance",
      instance_uid: "test-uid",
      target: "production/Pod/payments-123",
      started_at: null,
      ended_at: null,
      rule_id: "test-witness",
    },
  ];
  d.leader_instance_resolution = "UNKNOWN";
  d.investigation_audit = null;
  d.timing = { status: "UNASSESSED", withheld: [], drivers: [] };
  d.evidence = Array.from({ length: 95 }, (_, index) => ({
    ...d.initiating_findings[0],
    summary: `Test observation ${index}`,
    evidence_ids: [`test-ref-${index}`],
  }));
  d.causal_path = [
    {
      source: "ns/Deployment/source-a",
      target: "ns/Pod/target-a",
      relation: "engine-relation-a",
      direction: "FORWARD",
    },
    {
      source: "ns/Service/disjoint-source",
      target: "ns/Pod/disjoint-target",
      relation: "engine-relation-b",
      direction: "FORWARD",
    },
  ];
  await page.route(`**/api/v1/console/incidents/${d.incident_id}`, (route) =>
    route.fulfill({ json: data }),
  );
  await page.route(
    `**/api/v1/console/incidents/${d.incident_id}/evidence`,
    (route) => route.fulfill({ json: [] }),
  );
  await open(page, `/incidents/${d.incident_id}`);
  await expect(page.getByText("2 competing actors")).toBeVisible();
  await expect(page.getByText(/Executed by .*time unknown/)).toBeVisible();
  await expect(
    page.getByText("Exact instance not determined"),
  ).not.toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Select graph resource ns/Service/disjoint-source",
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("tab", { name: /^Evidence/ }).click();
  await expect(
    page.getByRole("button", { name: "Show 30 more findings" }),
  ).toBeVisible();
  await page.getByLabel("Search findings").fill("Test observation 94");
  await page.getByRole("button", { name: /Test observation 94/ }).click();
  await expect(
    page.getByText(/No captured raw provenance matches/),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await noPageOverflow(page);
  d.leading_actor_display = "NOT_ESTABLISHED";
  d.leading_root_actor = null;
  d.resolution = "INSUFFICIENT_EVIDENCE";
  await page.reload();
  await page.getByRole("tab", { name: "Diagnosis", exact: true }).click();
  await expect(
    page.getByText("Not established", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText(/Executed by/)).not.toBeVisible();
});

test("real seeded screens fit desktop, laptop, tablet and mobile in both themes", async ({
  page,
}) => {
  const data = await incident(page);
  for (const width of [1920, 1440, 1100, 768, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const theme of ["dark", "light"]) {
      await page.addInitScript(
        (theme) => localStorage.setItem("agentic-sre-theme", theme),
        theme,
      );
      for (const path of [
        "",
        "/incidents",
        `/incidents/${data.incident.incident_id}`,
        "/changes",
        "/reports",
        "/connections",
        "/settings",
      ]) {
        await open(page, path);
        await expect(page.locator("main h1")).toBeVisible();
        await expect(page.locator("main .animate-pulse")).toHaveCount(0);
        await noPageOverflow(page);
      }
    }
  }
});

test("mobile navigation includes theme control and restores focus", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, "");
  const trigger = page.getByRole("button", { name: "Open navigation" });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Navigation" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Light theme" }).click();
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
});

test("single actor execution, exact instance, and assessed timing follow engine fields", async ({
  page,
}) => {
  const data = await incident(page);
  const d = data.diagnosis!;
  d.leading_actor_display = "SINGLE";
  d.resolution = "AMBIGUOUS";
  d.is_resolved = false;
  d.confidence = "VERIFIED";
  d.leader_instance_resolution = "EXACT";
  d.leader_instance = "prod/Pod/exact-actor-instance";
  d.leader_instance_uid = "12345678-full-uid";
  d.executing_instances = [];
  d.timing = { status: "STABLE", withheld: [], drivers: [] };
  await page.route(`**/api/v1/console/incidents/${d.incident_id}`, (route) =>
    route.fulfill({ json: data }),
  );
  await open(page, `/incidents/${d.incident_id}`);
  await expect(
    page.getByText(
      /Exact instance: prod\/Pod\/exact-actor-instance \(UID 12345678\)/,
    ),
  ).toBeVisible();
  await expect(
    page.getByText("Holds over every admissible onset"),
  ).toBeVisible();
  await expect(page.getByText("Uncertainty remains.")).toBeVisible();
  d.leader_instance_resolution = "MULTIPLE_VIABLE";
  d.timing = {
    status: "SENSITIVE",
    withheld: [
      {
        actor: d.leading_root_actor!,
        authority: "TEST_AUTHORITY",
        relations: ["TEST_RELATION"],
      },
    ],
    drivers: [
      {
        onset: d.onset!,
        diagnosis_status: "TEST_STATUS",
        actor: d.leading_root_actor!,
        change: "TEST_CHANGE",
        reason: "Test timing sensitivity reason",
      },
    ],
  };
  await page.reload();
  await expect(
    page.getByText("Several instances remain possible"),
  ).toBeVisible();
  await page.getByRole("tab", { name: /^Investigation/ }).click();
  await expect(page.getByText("Test timing sensitivity reason")).toBeVisible();
  d.executing_instances = [
    {
      actor: d.leading_root_actor!,
      instance: "prod/Pod/executing-instance",
      instance_uid: "witness-uid",
      target: "prod/Pod/target",
      started_at: null,
      ended_at: "2026-10-08T22:18:00Z",
      rule_id: "test-rule",
    },
  ];
  await page.reload();
  await page.getByRole("tab", { name: "Diagnosis", exact: true }).click();
  await expect(page.getByText(/start unknown, until/)).toBeVisible();
  await expect(
    page.getByText("Several instances remain possible"),
  ).not.toBeVisible();
});

test("audit disclosures retain authorization, evidence novelty, hypothesis effects and stop reason", async ({
  page,
}) => {
  const data = await incident(page);
  const d = data.diagnosis!;
  d.competing_hypotheses = [
    {
      hypothesis_id: "test-hypothesis",
      actor: "test/Deployment/candidate",
      epistemic_state: "SUPPORTED",
      score: 17,
      causal_explanation: "Explicit test hypothesis explanation",
      reasons: ["Explicit test reason"],
    },
  ];
  d.investigation_audit = {
    diagnosis_run_id: "test-run",
    artifact_version: "test-version",
    initial_resolution: "AMBIGUOUS",
    final_resolution: "RESOLVED",
    stop_reason: "TEST_STOP",
    turns: 1,
    model_calls: 0,
    tool_calls: 1,
    action_audits: [
      {
        turn_index: 1,
        gap_id: "test-gap",
        gap_dimension: "test-dimension",
        missing_fact: "Test missing fact",
        intent_id: "test-intent",
        intent_kind: "test-kind",
        action: "test-read",
        capability: "test-capability",
        target: "prod/Pod/target",
        action_rationale: "Test read rationale",
        authorization_result: "ALLOWED",
        authorization_reason: "Test authorization reason",
        backend_execution_status: "SUCCEEDED",
        observation_id: "test-observation",
        observation_outcome: "Test observation outcome",
        returned_evidence_refs: [
          "new-ref",
          "known-ref",
          ...d.initiating_findings[0].evidence_ids,
        ],
        new_evidence_refs: ["new-ref"],
        already_known_refs: ["known-ref"],
        normalized_finding_ids: ["test-finding"],
        affected_hypothesis_ids: ["test-hypothesis", "absent-hypothesis"],
        resolution_before: "AMBIGUOUS",
        resolution_after: "RESOLVED",
        decision_state_changed: true,
        progress_classification: "Test progress",
      },
    ],
  };
  await page.route(`**/api/v1/console/incidents/${d.incident_id}`, (route) =>
    route.fulfill({ json: data }),
  );
  await open(page, `/incidents/${d.incident_id}`);
  await page.getByRole("tab", { name: /^Investigation/ }).click();
  await expect(page.getByText("TEST_STOP")).toBeVisible();
  await page.locator("summary").filter({ hasText: "test-read" }).click();
  for (const value of [
    "Test read rationale",
    "Test authorization reason",
    "Test observation outcome",
    "Test missing fact",
    "test-hypothesis",
  ])
    await expect(page.getByText(value, { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: /^Decision changed/ }).click();
  await expect(
    page.locator("summary").filter({ hasText: "test-read" }),
  ).toBeVisible();
  await page.getByRole("tab", { name: /^New evidence/ }).click();
  await expect(
    page.locator("summary").filter({ hasText: "test-read" }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Diagnosis", exact: true }).click();
  await page
    .getByRole("button", { name: /^Inspect finding:/ })
    .first()
    .click();
  await page.getByRole("button", { name: /Open investigation turn/ }).click();
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(page).toHaveURL(/tab=trace/);
  await expect(page).toHaveURL(/turn=1/);
  const target = page.locator("summary").filter({ hasText: "test-read" });
  await expect(target).toBeFocused();
  await expect(
    page.getByText("Test read rationale", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(target).toBeFocused();
  await expect(
    page.getByText("Test read rationale", { exact: true }),
  ).toBeVisible();
  const hypothesisLink = page.getByRole("button", {
    name: "test-hypothesis",
    exact: true,
  });
  await expect(
    page.getByRole("button", { name: /absent-hypothesis/ }),
  ).not.toBeVisible();
  await hypothesisLink.click();
  await expect(
    page.getByRole("dialog", { name: "Hypothesis inspector" }),
  ).toBeVisible();
  await expect(
    page.getByText("Explicit test hypothesis explanation", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Explicit test reason", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(hypothesisLink).toBeFocused();
  expect(new URL(page.url()).searchParams.has("hypothesis")).toBe(false);
});

test("server filters retain pagination and the report preview uses an immutable snapshot", async ({
  page,
}) => {
  const data = await incident(page);
  await open(page, "/incidents");
  const request = page.waitForRequest(
    (request) =>
      request.url().includes("/console/incidents?") &&
      request.url().includes("severity=CRITICAL"),
  );
  await page.getByRole("combobox", { name: "Severity", exact: true }).click();
  await page.getByRole("option", { name: "Critical", exact: true }).click();
  const url = new URL((await request).url());
  expect(url.searchParams.get("offset")).toBe("0");
  expect(url.searchParams.get("limit")).toBe("25");
  await expect(
    page.getByRole("combobox", { name: "Severity", exact: true }),
  ).toBeFocused();
  const confidence = page.getByRole("combobox", {
    name: "Confidence",
    exact: true,
  });
  await confidence.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("listbox")).toBeVisible();
  await expect(
    page.getByRole("option", { name: "Any", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(confidence).toBeFocused();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(
    page.getByRole("combobox", { name: "Severity", exact: true }),
  ).toContainText("Any");
  await open(page, `/incidents/${data.incident.incident_id}`);
  await page.getByRole("tab", { name: "Reports", exact: true }).click();
  await page.getByRole("button", { name: "Generate report" }).click();
  await page.getByRole("button", { name: "Preview", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "Report preview" }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("dialog")
      .getByText(data.incident.title, { exact: false })
      .first(),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await open(page, "/reports");
  await expect(page.getByText(/Run /).first()).toBeVisible();
  await page
    .getByRole("button", { name: "Share", exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("dialog", { name: "Share immutable report" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Send", exact: true }),
  ).toBeDisabled();
});

test("inset sidebar collapses accessibly and retains full mobile navigation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await open(page, "");
  const toggle = page.getByRole("button", { name: "Toggle sidebar" });
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  const navigation = page.getByRole("navigation", {
    name: "Primary navigation",
  });
  for (const name of ["Connections", "Settings"]) {
    const link = navigation.getByRole("link", { name, exact: true });
    await expect(link).toBeVisible();
    await expect(link).toHaveText("");
    await expect(link.locator("svg")).toBeVisible();
  }
  await toggle.click();
  for (const name of ["Connections", "Settings"]) {
    await expect(
      navigation.getByRole("link", { name, exact: true }),
    ).toHaveText(name);
  }
  await toggle.click();
  await expect(
    page
      .getByRole("navigation", { name: "Primary navigation" })
      .getByRole("link", { name: "Incidents", exact: true }),
  ).toBeVisible();
  await noPageOverflow(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(
    page
      .getByRole("dialog")
      .getByRole("link", { name: "Connections", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
});

test("raw provenance is joined only by explicit IDs and long observations stay inside the inspector", async ({
  page,
}) => {
  const data = await incident(page);
  const finding = data.diagnosis!.initiating_findings[0];
  finding.evidence_ids = ["matching-test-id"];
  await page.route(
    `**/api/v1/console/incidents/${data.incident.incident_id}`,
    (route) => route.fulfill({ json: data }),
  );
  await page.route(
    `**/api/v1/console/incidents/${data.incident.incident_id}/evidence`,
    (route) =>
      route.fulfill({
        json: [
          {
            evidence_id: "matching-test-id",
            source_type: "test-type",
            source_system: "test-source",
            collected_at: "2026-10-08T22:20:00Z",
            starts_at: "2026-10-08T22:18:00Z",
            ends_at: "2026-10-08T22:19:00Z",
            raw_result_reference: "test-reference",
            observation: { longIdentifier: "long-observation-".repeat(100) },
          },
          {
            evidence_id: "unrelated-test-id",
            source_type: "test-type",
            source_system: "Unrelated source must stay hidden",
            collected_at: "2026-10-08T22:20:00Z",
            starts_at: "2026-10-08T22:18:00Z",
            ends_at: "2026-10-08T22:19:00Z",
            raw_result_reference: "unrelated",
            observation: {},
          },
        ],
      }),
  );
  await open(page, `/incidents/${data.incident.incident_id}`);
  await page
    .getByRole("button", { name: /^Inspect finding:/ })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "Evidence inspector" });
  await expect(dialog.getByText("test-source · test-type")).toBeVisible();
  await expect(
    dialog.getByText("Unrelated source must stay hidden"),
  ).not.toBeVisible();
  await dialog
    .locator("summary")
    .filter({ hasText: "Raw observation" })
    .click();
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBeTruthy();
  await page.keyboard.press("Escape");
});

test("URL filters and evidence group survive reload without changing API values", async ({
  page,
}) => {
  await open(
    page,
    "/incidents?severity=CRITICAL&confidence=VERIFIED&offset=25",
  );
  await expect(
    page.getByRole("combobox", { name: "Severity", exact: true }),
  ).toContainText("Critical");
  await page
    .getByRole("textbox", { name: "Service", exact: true })
    .fill("order-service");
  await expect(page).toHaveURL(/service=order-service/);
  expect(new URL(page.url()).searchParams.has("offset")).toBe(false);
  await page.reload();
  await expect(
    page.getByRole("textbox", { name: "Service", exact: true }),
  ).toHaveValue("order-service");
  await expect(
    page.getByRole("combobox", { name: "Confidence", exact: true }),
  ).toContainText("Verified");
  await page.getByRole("button", { name: "Clear filters" }).click();
  expect(new URL(page.url()).searchParams.size).toBe(0);
  await open(
    page,
    "/changes?scope=CONFIGURATION&change_type=UPDATED&q=checkout",
  );
  await expect(
    page.getByRole("combobox", { name: "Scope", exact: true }),
  ).toContainText("Configuration");
  await page.reload();
  await expect(
    page.getByRole("searchbox", { name: "Search resource" }),
  ).toHaveValue("checkout");
  const data = await incident(page);
  await open(page, `/incidents/${data.incident.incident_id}?tab=evidence`);
  await page.getByRole("tab", { name: /^Supporting/ }).click();
  await page.getByLabel("Search findings").fill("payment");
  await page.reload();
  await expect(page.getByRole("tab", { name: /^Evidence/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("tab", { name: /^Supporting/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByLabel("Search findings")).toHaveValue("payment");
  await noPageOverflow(page);
});

test("copy controls copy exact identity and report search distinguishes empty results", async ({
  page,
}) => {
  const data = await incident(page);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          document.documentElement.dataset.copied = value;
        },
      },
    });
  });
  await open(page, `/incidents/${data.incident.incident_id}`);
  await page
    .getByRole("button", { name: "Copy incident ID", exact: true })
    .click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-copied",
    data.incident.incident_id,
  );
  await page
    .getByRole("button", { name: /^Inspect finding:/ })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Copy evidence ID", exact: true })
    .first()
    .click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-copied",
    data.diagnosis!.initiating_findings[0].evidence_ids[0],
  );
  await page.keyboard.press("Escape");
  await open(page, "/reports");
  const trigger = page.getByRole("button", { name: "Export report" }).first();
  await trigger.click();
  const pdf = page.getByRole("menuitem", { name: "PDF document" });
  await expect(pdf).toHaveAttribute("href", /\/reports\/.+\/pdf$/);
  await expect(
    page.getByRole("menuitem", { name: "Markdown", exact: true }),
  ).toHaveAttribute("href", /\/markdown$/);
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await page
    .getByRole("searchbox", { name: "Search reports" })
    .fill("no-report-can-match-this-string");
  await expect(
    page.getByText("No matching reports.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No reports yet.", { exact: true }),
  ).not.toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("searchbox", { name: "Search reports" }),
  ).toHaveValue("no-report-can-match-this-string");
});

test("incident return restores explorer filters and pagination; status explanations restore focus", async ({
  page,
}) => {
  const data = await incident(page);
  await page.route("**/api/v1/console/incidents?**", (route) =>
    route.fulfill({
      json: { items: [data.incident], total: 50, limit: 25, offset: 25 },
    }),
  );
  const query = "severity=CRITICAL&confidence=VERIFIED&offset=25&q=order";
  await open(page, `/incidents?${query}`);
  const badge = page.getByRole("button", { name: "VERIFIED: explain status" });
  await badge.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("dialog", { name: "VERIFIED explanation" }),
  ).toBeVisible();
  await expect(
    page.getByText(/Confidence is separate from resolution/),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(badge).toBeFocused();
  await page
    .getByRole("link", { name: data.incident.title, exact: true })
    .click();
  await expect(
    page.getByText("Deterministic diagnosis", { exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: /^Evidence/ }).click();
  await page.reload();
  await page.getByRole("link", { name: "All incidents" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/incidents\\?${query.replaceAll("&", "\\&")}$`),
  );
  await expect(
    page.getByRole("combobox", { name: "Severity", exact: true }),
  ).toContainText("Critical");
  await expect(
    page.getByRole("searchbox", { name: "Search title" }),
  ).toHaveValue("order");
  expect(new URL(page.url()).searchParams.get("offset")).toBe("25");
  await open(
    page,
    `/incidents/${data.incident.incident_id}?returnTo=https%3A%2F%2Fexample.com`,
  );
  await expect(
    page.getByRole("link", { name: "All incidents" }),
  ).toHaveAttribute("href", /\/app\/incidents$/);
});

test("chronology groups equal timestamps, separates unknown time, and report IDs are searchable", async ({
  page,
}) => {
  const data = await incident(page);
  const at = "2026-10-08T22:18:00Z";
  data.diagnosis!.onset = at;
  data.diagnosis!.evidence = data.diagnosis!.evidence.map((finding) => ({
    ...finding,
    at,
  }));
  data.timeline = {
    ...data.timeline,
    alert_fired: at,
    incident_opened: "2026-10-08T22:18:05Z",
    diagnosis_ready: null,
    phases: [],
    events: [],
  };
  await page.route(
    `**/api/v1/console/incidents/${data.incident.incident_id}`,
    (route) => route.fulfill({ json: data }),
  );
  await page.route(
    `**/api/v1/console/incidents/${data.incident.incident_id}/changes`,
    (route) =>
      route.fulfill({
        json: [
          {
            change_id: "earlier",
            timestamp: "2026-10-08T22:17:00Z",
            resource_type: "Deployment",
            resource_name: "test-earlier",
            change_type: "UPDATED",
            scope: "DEPLOYMENT",
            source: "test",
            revision: null,
            onset_delta_seconds: -60,
            matches_leading_actor: false,
          },
          {
            change_id: "unknown",
            timestamp: "",
            resource_type: "Deployment",
            resource_name: "test-unknown",
            change_type: "UPDATED",
            scope: "DEPLOYMENT",
            source: "test",
            revision: null,
            onset_delta_seconds: null,
            matches_leading_actor: false,
          },
        ],
      }),
  );
  await open(page, `/incidents/${data.incident.incident_id}?tab=timeline`);
  const chronology = page.getByRole("list", {
    name: "Timestamped investigation chronology",
  });
  await expect(
    chronology
      .getByRole("listitem")
      .filter({ hasText: "Deployment/test-earlier" })
      .first(),
  ).toBeVisible();
  const groups = chronology.locator(":scope > li");
  await expect(groups).toHaveCount(3);
  await expect(groups.nth(0)).toContainText("test-earlier");
  await expect(groups.nth(1)).toContainText("Alert fired");
  await expect(groups.nth(1)).toContainText("Incident onset");
  await expect(
    chronology.getByText("Deployment/test-unknown"),
  ).not.toBeVisible();
  await expect(
    page.getByText("Deployment/test-unknown", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/Temporal proximity does not establish causation/),
  ).toBeVisible();
  const reports = await (
    await page.request.get("/api/v1/console/reports")
  ).json();
  const report = reports[0];
  expect(report).toBeTruthy();
  await open(page, `/reports?q=${encodeURIComponent(report.report_id)}`);
  await expect(
    page.getByRole("link", { name: report.title, exact: true }),
  ).toHaveCount(1);
  await page
    .getByRole("searchbox", { name: "Search reports" })
    .fill(report.diagnosis_run_id);
  await expect(
    page.getByText("No matching reports.", { exact: true }),
  ).not.toBeVisible();
  await expect(
    page.getByRole("link", { name: report.title, exact: true }).first(),
  ).toBeVisible();
});

test("notification center retains burst history beyond toasts, persists read state, and works on mobile", async ({
  page,
}) => {
  await page.addInitScript(() => {
    if (!localStorage.getItem("notification-test-initialized")) {
      localStorage.setItem(
        "agentic-sre.notifications.v1",
        JSON.stringify({ incidents: [], diagnosed: [] }),
      );
      localStorage.setItem("notification-test-initialized", "true");
    }
  });
  await open(page, "");
  const bell = page.getByRole("button", { name: /^Notifications/ });
  await expect(bell).toHaveAccessibleName("Notifications, 5 unread incidents");
  await page
    .getByRole("button", { name: "Dismiss notification" })
    .first()
    .click();
  await bell.click();
  const center = page.getByRole("dialog", { name: "Notification center" });
  await expect(center.getByRole("listitem")).toHaveCount(5);
  await expect(center.getByText(/This browser/)).toBeVisible();
  await center
    .getByRole("list")
    .getByRole("button", { name: /^Mark .* read$/ })
    .first()
    .click();
  await expect(bell).toHaveAccessibleName("Notifications, 4 unread incidents");
  await page.keyboard.press("Escape");
  await expect(bell).toBeFocused();
  await page.reload();
  await expect(bell).toHaveAccessibleName("Notifications, 4 unread incidents");
  await page.setViewportSize({ width: 390, height: 844 });
  await bell.click();
  await center
    .getByRole("button", { name: "Unread only", exact: true })
    .click();
  await expect(center.getByRole("listitem")).toHaveCount(4);
  await center.getByRole("button", { name: "Mark all read" }).click();
  await expect(
    center.getByText("No unread notifications.", { exact: true }),
  ).toBeVisible();
  await expect(bell).toHaveAccessibleName("Notifications");
  await center
    .getByRole("button", { name: "Unread only", exact: true })
    .click();
  await expect(center.getByRole("listitem")).toHaveCount(5);
  await noPageOverflow(page);
  const link = center.getByRole("link").first();
  const title = await link.textContent();
  await link.click();
  await expect(center).not.toBeVisible();
  await expect(
    page.getByRole("heading", { name: title!, exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(bell).toHaveAccessibleName("Notifications");
});

test("first visit notification center starts empty without announcing existing incidents", async ({
  page,
}) => {
  await open(page, "");
  await page
    .getByRole("button", { name: "Notifications", exact: true })
    .click();
  await expect(
    page.getByText("No notifications recorded yet.", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Notifications", exact: true }),
  ).toBeFocused();
});
