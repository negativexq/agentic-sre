import { describe, expect, it } from "vitest";

import type { IncidentListItem } from "@/api/types";
import { MAX_BURST, diffIncidents, type SeenState } from "@/notifications/diff";

function incident(id: string, overrides: Partial<IncidentListItem> = {}): IncidentListItem {
  return {
    incident_id: id,
    title: `Alert ${id}`,
    status: "OPEN",
    severity: "WARNING",
    source: "alertmanager",
    service: "payment-service",
    leading_root_actor: null,
    confidence: null,
    resolution: null,
    has_diagnosis: false,
    created_at: `2026-09-30T10:00:0${id.length > 1 ? 9 : Number(id) % 10}Z`,
    updated_at: "2026-09-30T10:10:00Z",
    age_seconds: 60,
    ...overrides,
  };
}

const seenBoth = (incidents: string[], diagnosed: string[] = []): SeenState => ({ incidents, diagnosed });

describe("diffIncidents", () => {
  it("takes the first visit as a silent baseline, whatever the state", () => {
    const result = diffIncidents(null, [incident("1"), incident("2", { has_diagnosis: true })]);
    expect(result.notifications).toEqual([]);
    expect(result.overflow).toBe(0);
    expect(result.seen).toEqual({ incidents: ["1", "2"], diagnosed: ["2"] });
  });

  it("announces an incident the operator has not seen, once", () => {
    const first = diffIncidents(seenBoth(["1"]), [incident("1"), incident("2")]);
    expect(first.notifications.map((n) => [n.kind, n.incidentId])).toEqual([["incident", "2"]]);
    const again = diffIncidents(first.seen, [incident("1"), incident("2")]);
    expect(again.notifications).toEqual([]);
  });

  it("counts an incident once even when it is announced twice, as new and then as diagnosed", () => {
    const first = diffIncidents(seenBoth([]), [incident("1")]);
    const second = diffIncidents(first.seen, [incident("1", { has_diagnosis: true })]);
    expect(first.announcedIncidentIds).toEqual(["1"]);
    expect(second.announcedIncidentIds).toEqual(["1"]);
    expect(new Set([...first.announcedIncidentIds, ...second.announcedIncidentIds]).size).toBe(1);
  });

  it("announces the diagnosis when it turns up on an incident already being watched", () => {
    const watching = seenBoth(["1"]);
    const stored = diffIncidents(watching, [
      incident("1", { has_diagnosis: true, resolution: "AMBIGUOUS", leading_root_actor: "sre-demo/NetworkChaos/x", confidence: "LIKELY" }),
    ]);
    const [notice] = stored.notifications;
    expect(notice).toMatchObject({
      kind: "diagnosis",
      incidentId: "1",
      resolution: "AMBIGUOUS",
      leadingActor: "sre-demo/NetworkChaos/x",
    });
    expect(diffIncidents(stored.seen, [incident("1", { has_diagnosis: true })]).notifications).toEqual([]);
  });

  it("gives an incident that already carries its diagnosis one notice, not two", () => {
    const result = diffIncidents(seenBoth(["1"]), [incident("1"), incident("2", { has_diagnosis: true })]);
    expect(result.notifications).toHaveLength(1);
    expect(result.notifications[0]).toMatchObject({ kind: "incident", incidentId: "2", diagnosed: true });
    expect(result.seen.diagnosed).toContain("2");
  });

  it("does not announce a re-run diagnosis of an incident whose diagnosis was already reported", () => {
    const result = diffIncidents(seenBoth(["1"], ["1"]), [incident("1", { has_diagnosis: true, resolution: "RESOLVED" })]);
    expect(result.notifications).toEqual([]);
  });

  it("folds a burst into the newest few and counts the rest", () => {
    const items = ["1", "2", "3", "4", "5"].map((id, i) =>
      incident(id, { created_at: `2026-09-30T10:00:0${i}Z` }),
    );
    const result = diffIncidents(seenBoth([]), items);
    expect(result.notifications.map((n) => n.incidentId)).toEqual(["5", "4", "3"].slice(0, MAX_BURST));
    expect(result.overflow).toBe(2);
    expect(result.allNotifications).toHaveLength(5);
    expect(result.announcedIncidentIds).toHaveLength(5); // the unread count covers the folded ones too
    expect(result.seen.incidents).toHaveLength(5); // all are seen, so none returns on the next poll
  });

  it("remembers a bounded number of ids, dropping the oldest", () => {
    const many = Array.from({ length: 600 }, (_, i) => incident(`i${i}`, { created_at: `2026-09-30T10:${String(i % 60).padStart(2, "0")}:00Z` }));
    const result = diffIncidents(seenBoth([]), many);
    expect(result.seen.incidents.length).toBe(500);
  });

  it("orders announcements newest first", () => {
    const result = diffIncidents(seenBoth([]), [
      incident("old", { created_at: "2026-09-30T09:00:00Z" }),
      incident("new", { created_at: "2026-09-30T11:00:00Z" }),
    ]);
    expect(result.notifications.map((n) => n.incidentId)).toEqual(["new", "old"]);
  });

  it("carries only what the persisted incident says", () => {
    const [notice] = diffIncidents(seenBoth([]), [
      incident("1", { severity: "CRITICAL", service: null, title: "HighRequestLatency" }),
    ]).notifications;
    expect(notice).toMatchObject({ title: "HighRequestLatency", severity: "CRITICAL", service: null, resolution: null });
  });
});
