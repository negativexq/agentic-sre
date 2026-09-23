import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { ChangeFilters, IncidentFilters } from "@/api/types";

// SSE (useLiveUpdates) drives real-time refetches; the intervals below are a
// fallback for when EventSource is unavailable or a stream drops.
const FALLBACK_INTERVAL = 30_000;

export function useDashboard() {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard,
    refetchInterval: FALLBACK_INTERVAL,
  });
}

export function useIncidents(filters: IncidentFilters) {
  return useQuery({
    queryKey: ["incidents", filters],
    queryFn: () => api.incidents(filters),
    refetchInterval: FALLBACK_INTERVAL,
  });
}

export function useIncident(id: string | undefined) {
  return useQuery({
    queryKey: ["incident", id],
    queryFn: () => api.incident(id as string),
    enabled: Boolean(id),
    refetchInterval: FALLBACK_INTERVAL,
  });
}

export function useIncidentEvidence(id: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["incident-evidence", id],
    queryFn: () => api.incidentEvidence(id as string),
    enabled: Boolean(id) && enabled,
  });
}

export function useChanges(filters: ChangeFilters) {
  return useQuery({
    queryKey: ["changes", filters],
    queryFn: () => api.changes(filters),
    refetchInterval: FALLBACK_INTERVAL,
  });
}

export function useIncidentChanges(id: string | undefined) {
  return useQuery({
    queryKey: ["incident-changes", id],
    queryFn: () => api.incidentChanges(id as string),
    enabled: Boolean(id),
  });
}

export function useReports() {
  return useQuery({
    queryKey: ["reports"],
    queryFn: api.reports,
    refetchInterval: FALLBACK_INTERVAL,
  });
}

export function useIncidentReports(id: string | undefined) {
  return useQuery({
    queryKey: ["incident-reports", id],
    queryFn: () => api.incidentReports(id as string),
    enabled: Boolean(id),
  });
}

export function useCreateReport(incidentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.createReport(incidentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["incident-reports", incidentId] });
      void queryClient.invalidateQueries({ queryKey: ["reports"] });
    },
  });
}

export function useReportDeliveries(reportId: string | undefined) {
  return useQuery({
    queryKey: ["deliveries", reportId],
    queryFn: () => api.reportDeliveries(reportId as string),
    enabled: Boolean(reportId),
  });
}

export function useShareReport(reportId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (request: { recipients: string[]; include_pdf: boolean }) =>
      api.shareReport(reportId, request),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["deliveries", reportId] });
    },
  });
}

export function useSystemStatus() {
  return useQuery({
    queryKey: ["system"],
    queryFn: api.system,
    refetchInterval: 15_000,
  });
}

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: api.settings,
  });
}
