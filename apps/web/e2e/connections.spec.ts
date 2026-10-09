import { test, expect, type Page } from "@playwright/test";
import type {
  ConnectorsView,
  CreatedConnector,
  PreflightCheck,
  SystemStatus,
} from "../src/api/types";

// The Connections page against synthetic DTOs (docs/ui/connect-cluster-design.md §5).

async function open(page: Page, path: string) {
  await page.goto(
    `${process.env.WEB_E2E_URL ?? "http://localhost:5173"}${path}`,
  );
}

const SYSTEM: SystemStatus = {
  mode: "connector",
  connectors: [
    { name: "Database", status: "connected", detail: null },
    {
      name: "Prometheus",
      status: "not_configured",
      detail:
        "not configured on the Connector: set backends.prometheus.url in its values",
    },
  ],
};

const ACTIVE = {
  id: "lab",
  status: "active",
  connected: true,
  certificate_valid_until: "2027-01-07T10:00:00Z",
  renews_after: "2026-12-08T10:00:00Z",
  enrolled_at: "2026-10-08T10:00:00Z",
  created_at: "2026-10-08T09:59:00Z",
};

const CHECKS: PreflightCheck[] = [
  { name: "alertmanager", status: "ok", detail: "/api/v2/status: ready" },
  {
    name: "rbac.read",
    status: "failed",
    detail: "missing: list apps/deployments in shop",
  },
  {
    name: "rbac.read-only",
    status: "warning",
    detail: "broader than read-only: get secrets",
  },
];

const CREATED: CreatedConnector = {
  id: "lab2",
  token: "lab2.s3cret.Q0E",
  expires_at: "2026-10-09T12:00:00Z",
  install_command:
    "helm upgrade --install lab2 charts/agentic-sre-connector \\\n  --set enrollment.token=lab2.s3cret.Q0E",
  endpoints_configured: false,
};

async function mock(
  page: Page,
  options: { writable?: boolean; system?: SystemStatus } = {},
) {
  const state: ConnectorsView = {
    available: true,
    writable: options.writable ?? true,
    endpoints_configured: false,
    connectors: [ACTIVE],
  };
  const calls = { disabled: [] as string[], preflight: 0 };
  await page.route("**/api/v1/console/system", (route) =>
    route.fulfill({ json: options.system ?? SYSTEM }),
  );
  await page.route("**/api/v1/console/connectors", async (route) => {
    if (route.request().method() === "POST") {
      state.connectors.push({
        ...ACTIVE,
        id: "lab2",
        status: "pending",
        connected: false,
        certificate_valid_until: null,
        renews_after: null,
        enrolled_at: null,
      });
      return route.fulfill({ status: 201, json: CREATED });
    }
    return route.fulfill({ json: state });
  });
  await page.route("**/api/v1/console/connectors/*/preflight", (route) => {
    calls.preflight += 1;
    return route.fulfill({ json: CHECKS });
  });
  await page.route("**/api/v1/console/connectors/*/disable", (route) => {
    const id = route.request().url().split("/").at(-2) ?? "";
    calls.disabled.push(id);
    const row = state.connectors.find((c) => c.id === id);
    if (row) Object.assign(row, { status: "disabled", connected: false });
    return route.fulfill({ status: 204, body: "" });
  });
  return {
    state,
    calls,
    connect: (id: string) => {
      const row = state.connectors.find((c) => c.id === id);
      if (row) Object.assign(row, { status: "active", connected: true });
    },
  };
}

test("connect drawer: name, once-only token and command with copy, waiting, then preflight with reasons", async ({
  page,
}) => {
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
  const api = await mock(page);
  await open(page, "/connections");
  await page.getByRole("button", { name: "Connect a cluster" }).click();
  const drawer = page.getByRole("dialog", { name: "Connect a cluster" });
  await drawer.getByLabel("Connector name").fill("Bad_Name");
  await expect(drawer.getByText(/Not a DNS label/)).toBeVisible();
  await expect(
    drawer.getByRole("button", { name: "Create Connector" }),
  ).toBeDisabled();
  await drawer.getByLabel("Connector name").fill("lab2");
  await drawer.getByRole("button", { name: "Create Connector" }).click();

  await expect(drawer.getByTestId("connector-token")).toHaveText(CREATED.token);
  await expect(drawer.getByText(/will not be shown again/)).toBeVisible();
  await expect(drawer.getByText(/SRE_CONNECTOR_PUBLIC_ENDPOINT/)).toBeVisible();
  await drawer.getByRole("button", { name: "Copy token" }).click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-copied",
    CREATED.token,
  );
  await drawer.getByRole("button", { name: "Copy install command" }).click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-copied",
    CREATED.install_command,
  );

  await drawer.getByRole("button", { name: "I have run it" }).click();
  await expect(drawer.getByText(/Waiting for/)).toBeVisible();
  expect(api.calls.preflight).toBe(0);
  api.connect("lab2");
  await expect(
    drawer.getByText("missing: list apps/deployments in shop"),
  ).toBeVisible({ timeout: 10_000 });
  await expect(
    drawer.getByText("broader than read-only: get secrets"),
  ).toBeVisible();
  expect(api.calls.preflight).toBe(1);

  // Closing forgets the token: reopening starts again at the name.
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Connect a cluster" }).click();
  await expect(page.getByTestId("connector-token")).toHaveCount(0);
  await expect(page.getByLabel("Connector name")).toHaveValue("");
});

test("connectors table: on-demand preflight, disable with confirmation, fixed backend hint", async ({
  page,
}) => {
  const api = await mock(page);
  await open(page, "/connections");
  const row = page.getByRole("row", { name: /lab/ });
  await expect(row.getByText("Connected", { exact: true })).toBeVisible();
  await expect(page.getByText(/renews around/)).toBeVisible();
  expect(api.calls.preflight).toBe(0);
  await row.getByRole("button", { name: "Run preflight" }).click();
  await expect(
    page.getByText("missing: list apps/deployments in shop"),
  ).toBeVisible();

  await row.getByRole("button", { name: "Disable" }).click();
  const confirm = page.getByRole("dialog", { name: "Disable lab?" });
  await expect(confirm.getByText(/cannot be undone/)).toBeVisible();
  await confirm.getByRole("button", { name: "Cancel" }).click();
  expect(api.calls.disabled).toEqual([]);
  await row.getByRole("button", { name: "Disable" }).click();
  await page
    .getByRole("dialog", { name: "Disable lab?" })
    .getByRole("button", { name: "Disable" })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(api.calls.disabled).toEqual(["lab"]);
  await expect(row.getByText("disabled")).toBeVisible();

  await expect(
    page.getByText(
      "not configured on the Connector: set backends.prometheus.url in its values",
    ),
  ).toBeVisible();
  await expect(page.getByText(/PROMETHEUS_URL/)).toHaveCount(0);
});

test("without SRE_API_TOKEN the console offers no Connect or Disable", async ({
  page,
}) => {
  await mock(page, { writable: false });
  await open(page, "/connections");
  await expect(page.getByText(/needs SRE_API_TOKEN/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Connect a cluster" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Disable" })).toHaveCount(0);
});

test("demo badge shows only in demo mode, on every page, both themes and mobile", async ({
  page,
}) => {
  await mock(page, { system: { ...SYSTEM, mode: "demo" } });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const theme of ["dark", "light"]) {
      await page.addInitScript(
        (theme) => localStorage.setItem("agentic-sre-theme", theme),
        theme,
      );
      for (const path of ["", "/connections"]) {
        await open(page, path);
        await expect(
          page.getByText("Demo data", { exact: true }),
        ).toBeVisible();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
        ).toBeTruthy();
      }
      await expect(
        page.getByText(/no cluster is connected, by design/),
      ).toBeVisible();
    }
  }
  await page.unroute("**/api/v1/console/system");
  await page.route("**/api/v1/console/system", (route) =>
    route.fulfill({ json: SYSTEM }),
  );
  await open(page, "/connections");
  await expect(page.locator("main h1")).toBeVisible();
  await expect(page.getByText("Demo data", { exact: true })).toHaveCount(0);
});
