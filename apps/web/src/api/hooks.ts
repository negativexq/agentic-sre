import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { IncidentFilters } from "@/api/types";

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

export function useSystemStatus() {
  return useQuery({
    queryKey: ["system"],
    queryFn: api.system,
    refetchInterval: 15_000,
  });
}
