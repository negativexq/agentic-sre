import { lazy } from "react";
import { createBrowserRouter } from "react-router-dom";

import { Layout } from "@/app/Layout";

// Route-level code splitting keeps the initial bundle small; each page loads on
// demand behind the layout's Suspense fallback.
const OverviewPage = lazy(() =>
  import("@/pages/OverviewPage").then((m) => ({ default: m.OverviewPage })),
);
const IncidentsPage = lazy(() =>
  import("@/pages/IncidentsPage").then((m) => ({ default: m.IncidentsPage })),
);
const IncidentWorkspacePage = lazy(() =>
  import("@/pages/IncidentWorkspacePage").then((m) => ({ default: m.IncidentWorkspacePage })),
);
const ChangesPage = lazy(() =>
  import("@/pages/ChangesPage").then((m) => ({ default: m.ChangesPage })),
);
const ReportsPage = lazy(() =>
  import("@/pages/ReportsPage").then((m) => ({ default: m.ReportsPage })),
);
const ConnectionsPage = lazy(() =>
  import("@/pages/ConnectionsPage").then((m) => ({ default: m.ConnectionsPage })),
);
const SettingsPage = lazy(() =>
  import("@/pages/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);
const NotFoundPage = lazy(() =>
  import("@/pages/NotFoundPage").then((m) => ({ default: m.NotFoundPage })),
);

export const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <Layout />,
      children: [
        { index: true, element: <OverviewPage /> },
        { path: "incidents", element: <IncidentsPage /> },
        { path: "incidents/:incidentId", element: <IncidentWorkspacePage /> },
        { path: "changes", element: <ChangesPage /> },
        { path: "reports", element: <ReportsPage /> },
        { path: "connections", element: <ConnectionsPage /> },
        { path: "settings", element: <SettingsPage /> },
        { path: "*", element: <NotFoundPage /> },
      ],
    },
  ],
  { basename: import.meta.env.BASE_URL.replace(/\/$/, "") || undefined },
);
