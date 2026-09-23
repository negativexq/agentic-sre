import type {
  ChangeFilters,
  ChangeView,
  DashboardSummary,
  EvidenceView,
  IncidentDetail,
  IncidentFilters,
  IncidentPage,
  DeliveryView,
  ReportSummary,
  SettingsView,
  ShareRequest,
  SystemStatus,
} from "@/api/types";

const BASE = "/api/v1/console";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { error?: { message?: string } };
      if (body?.error?.message) message = body.error.message;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

function query(filters: IncidentFilters | ChangeFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

export const api = {
  dashboard: () => get<DashboardSummary>("/dashboard"),
  system: () => get<SystemStatus>("/system"),
  settings: () => get<SettingsView>("/settings"),
  incidents: (filters: IncidentFilters = {}) =>
    get<IncidentPage>(`/incidents${query(filters)}`),
  incident: (id: string) => get<IncidentDetail>(`/incidents/${id}`),
  incidentEvidence: (id: string) => get<EvidenceView[]>(`/incidents/${id}/evidence`),
  incidentChanges: (id: string) => get<ChangeView[]>(`/incidents/${id}/changes`),
  changes: (filters: ChangeFilters = {}) => get<ChangeView[]>(`/changes${query(filters)}`),
  createReport: async (id: string): Promise<{ report_id: string }> => {
    const response = await fetch(`${BASE}/incidents/${id}/reports`, {
      method: "POST",
      headers: { accept: "application/json" },
    });
    if (!response.ok) {
      throw new ApiError(response.status, `Could not create report (${response.status})`);
    }
    return (await response.json()) as { report_id: string };
  },
  incidentReports: (id: string) => get<ReportSummary[]>(`/incidents/${id}/reports`),
  reports: () => get<ReportSummary[]>("/reports"),
  reportDeliveries: (reportId: string) =>
    get<DeliveryView[]>(`/reports/${reportId}/deliveries`),
  shareReport: async (reportId: string, request: ShareRequest): Promise<DeliveryView> => {
    const response = await fetch(`${BASE}/reports/${reportId}/email`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(request),
    });
    if (!response.ok) {
      let message = `Could not share report (${response.status})`;
      try {
        const body = (await response.json()) as { detail?: string };
        if (body?.detail) message = body.detail;
      } catch {
        /* non-JSON */
      }
      throw new ApiError(response.status, message);
    }
    return (await response.json()) as DeliveryView;
  },
};

/** Absolute URL to a report export, safe to open or download directly. */
export function reportUrl(reportId: string, format: "markdown" | "pdf" | "json"): string {
  return `${BASE}/reports/${reportId}/${format}`;
}
