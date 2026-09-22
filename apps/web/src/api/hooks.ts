import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { IncidentFilters } from "@/api/types";

/** Dashboard refetches on an interval so "what is happening now" stays live. */
export function useDashboard() {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard,
    refetchInterval: 5_000,
  });
}

export function useIncidents(filters: IncidentFilters) {
  return useQuery({
    queryKey: ["incidents", filters],
    queryFn: () => api.incidents(filters),
    refetchInterval: 10_000,
  });
}

export function useIncident(id: string | undefined) {
  return useQuery({
    queryKey: ["incident", id],
    queryFn: () => api.incident(id as string),
    enabled: Boolean(id),
    refetchInterval: 5_000,
  });
}

export function useSystemStatus() {
  return useQuery({
    queryKey: ["system"],
    queryFn: api.system,
    refetchInterval: 15_000,
  });
}
