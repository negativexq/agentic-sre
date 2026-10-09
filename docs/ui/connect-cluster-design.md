# Connect a cluster from the console (roadmap D4)

Status: **approved by the owner (2026-10-09), every decision of §4 as recommended.** Builds on A8 (connector-install-design.md): the registry, the
one-time token, `preflight`, rotation, revocation and the chart. The console only reads and calls what A8 provides; it
adds no rule of its own.

## 1. Where it stands

- Connecting a cluster needs a terminal: `agentic-sre connector create <id>` for the token, then `helm install` with
  values typed by hand.
- The Connections page is one card of system status. It cannot list the registry, show a certificate's expiry beyond
  the 14-day warning, run `preflight`, or disable a Connector.
- The backend rows say "set PROMETHEUS_URL to enable" whenever a backend is not configured. That is the in-process
  setting of the control plane; with a Connector the place to configure it is the chart's values, so the hint sends a
  customer to the wrong place.
- The demo console (seeded data, `make console`) looks exactly like a real one, so "Not configured" there reads as a
  fault.

## 2. Screens

**Connections page** gets three sections:

1. **Connectors**: the registry as a table: id, status (`pending`, `active`, `disabled`), connected or not, certificate
   valid until (and "renews around" at two thirds of validity), last enrollment. Per row: **Run preflight** (connected
   only) and **Disable** (with a confirmation that says the live session closes within seconds and cannot be undone
   from the console).
2. **Connect a cluster** (a button opening a three-step drawer):
   1. *Name*: the Connector id (a DNS label, validated as the API does).
   2. *Install*: the one-time token and the exact `helm install` command, each with a copy button, shown **once**
      ("valid for 1 hour, single use; it will not be shown again"); the command carries the control plane's public
      endpoints, the chosen id's token, and placeholders for the namespaces and backend URLs to fill in.
   3. *Waiting*: polls until the Connector is connected, then runs `preflight` and shows its table; failures and
      warnings stay visible with their reason ("missing: list apps/deployments in shop"; "broader than read-only:
      get secrets").
3. **Data sources**: the backend rows as today, with the hint fixed: through a Connector, "not configured on the
   Connector: set `backends.prometheus.url` in its values"; in-process, the environment variable as now.

**Demo marker.** When the control plane runs on demo data, every page shows a "Demo data" badge in the header and the
Connections page says no cluster is connected by design.

## 3. API (all under `/api/v1/console/connectors`)

| Call | What it does |
|---|---|
| `GET /connectors` | the registry joined with the live sessions and their certificate expiry |
| `POST /connectors` `{id}` | creates the Connector and returns the token and the install command **once** |
| `POST /connectors/{id}/disable` | revokes (A8.3) |
| `POST /connectors/{id}/preflight` | runs `preflight` on that Connector over its session and returns the checks; reads only |

Writes use the existing bearer-token guard. `GET /system` adds `mode` (`connector`, `in-process`, `demo`).

## 4. Decisions for the owner

1. **Who may mint a token from the console.** The console has no user accounts yet; writes are guarded only by the
   optional shared token (`SRE_API_TOKEN`). Recommended: creating and disabling Connectors from the console is
   **refused unless `SRE_API_TOKEN` is set**, so an open console cannot hand out cluster access; the CLI keeps working
   either way. Alternative: always allowed, as other writes are.
2. **Public endpoints in the install command.** The control plane cannot know the address a customer's cluster
   reaches it at. Recommended: `SRE_CONNECTOR_PUBLIC_ENDPOINT` and `SRE_CONNECTOR_PUBLIC_ENROLL_ENDPOINT` (and an
   optional server name) set by the operator; when unset, the command shows placeholders and says so.
3. **Demo marker.** Recommended: `make console` sets `SRE_DEMO=true`; the badge appears only then.
4. **`preflight` from the console runs on demand only** (a button), never on a timer: each run asks the cluster's API
   for about a hundred access reviews. Recommended.

## 5. Validation

- API tests per call: the token returned once and never listed; refusal without the guard token (decision 1); disable
  closing a session; preflight relayed; `mode` per configuration.
- Playwright: the drawer's three steps, copy buttons, the once-only token, the waiting state, a failed preflight shown
  with its reasons, disable's confirmation, the fixed backend hint, the demo badge; both themes and mobile.
- Lab: a second Connector (`lab2`) created from the console, installed with the copied command into its own
  namespace, connected, preflight run from the console, then disabled from the console and uninstalled; `lab` and the
  `HOLDOUT` setup untouched.

## 6. Result (2026-10-09)

- API: 8 tests (the token once and never listed, 403 without `SRE_API_TOKEN`, disable, preflight relayed, `mode`).
  Playwright: 4 tests on synthetic DTOs (the drawer's three steps, copy buttons, the token forgotten on close, waiting
  then preflight with its reasons, disable's confirmation, the fixed hint, no write controls without the token, the
  demo badge in both themes and on mobile).
- Lab: `lab2` created through `POST /connectors` with the control plane temporarily given `SRE_API_TOKEN` and the
  public endpoints; the returned command, its placeholders filled, installed it into `agentic-sre-connector`; it
  enrolled and connected in about 15 s and the console listed it beside `lab`. Preflight passed every check but one:
  `read:chaos-mesh` failed although no evidence namespace was listed. The chart sets `SRE_EVIDENCE_NAMESPACES=""`;
  the Connector reads that as none, preflight read it as unset and fell back to `chaos-mesh`. Preflight now reads it
  as the Connector does (a test pins it). Disable answered 204 and closed the session in about 2 s; without the token
  writes answered 401. `lab2` was uninstalled and its namespace deleted; the control plane was restarted as before;
  `lab` stayed connected throughout.
- The browser token was not typed for the lab run, so the drawer itself was exercised only in Playwright; the lab
  run used the same API calls.
