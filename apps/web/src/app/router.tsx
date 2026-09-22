import { createBrowserRouter } from "react-router-dom";

import { Layout } from "@/app/Layout";
import { ChangesPage } from "@/pages/ChangesPage";
import { ConnectionsPage } from "@/pages/ConnectionsPage";
import { IncidentWorkspacePage } from "@/pages/IncidentWorkspacePage";
import { IncidentsPage } from "@/pages/IncidentsPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { OverviewPage } from "@/pages/OverviewPage";
import { ReportsPage } from "@/pages/ReportsPage";
import { SettingsPage } from "@/pages/SettingsPage";

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
