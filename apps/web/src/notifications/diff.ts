import type { IncidentListItem } from "@/api/types";

/**
 * Notifications are derived only from persisted incident state: an incident id that the operator has not
 * seen, or a `has_diagnosis` flag that turned true while the incident was being watched. Nothing here
 * invents progress the engine did not record (docs/ui/product-contract.md, G5).
 */

export type NotificationKind = "incident" | "diagnosis";

export interface AppNotification {
  id: string;
  kind: NotificationKind;
  incidentId: string;
  title: string;
  severity: string;
  service: string | null;
  /** True when the diagnosis was already stored the first time the incident was observed. */
  diagnosed: boolean;
  resolution: string | null;
  leadingActor: string | null;
  confidence: string | null;
}

/** What the operator has already been told about. Insertion order is age order. */
export interface SeenState {
  incidents: string[];
  diagnosed: string[];
}

export interface DiffResult {
  /** Newest first, at most `MAX_BURST`. */
  notifications: AppNotification[];
  /** How many further notifications were folded into a summary instead of being shown one by one. */
  overflow: number;
  /** Every incident announced in this comparison, shown or folded; the unread count is made from these. */
  announcedIncidentIds: string[];
  seen: SeenState;
}

export const MAX_BURST = 3;
const MAX_REMEMBERED = 500;

function remember(list: readonly string[], additions: readonly string[]): string[] {
  const merged = [...list.filter((id) => !additions.includes(id)), ...additions];
  return merged.slice(-MAX_REMEMBERED);
}

function notification(kind: NotificationKind, item: IncidentListItem): AppNotification {
  return {
    id: `${kind}:${item.incident_id}`,
    kind,
    incidentId: item.incident_id,
    title: item.title,
    severity: item.severity,
    service: item.service,
    diagnosed: item.has_diagnosis,
    resolution: item.resolution,
    leadingActor: item.leading_root_actor,
    confidence: item.confidence,
  };
}

/**
 * Compare a fresh incident page with what was already seen.
 *
 * `previous === null` means nothing was ever recorded (the first visit): the current state is taken as
 * the baseline and nothing is announced, so opening the console never floods the screen.
 */
export function diffIncidents(previous: SeenState | null, items: readonly IncidentListItem[]): DiffResult {
  const ordered = [...items].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const knownIncidents = new Set(previous?.incidents ?? []);
  const knownDiagnosed = new Set(previous?.diagnosed ?? []);

  const announced: AppNotification[] = [];
  if (previous !== null) {
    for (const item of ordered) {
      if (!knownIncidents.has(item.incident_id)) {
        announced.push(notification("incident", item));
      } else if (item.has_diagnosis && !knownDiagnosed.has(item.incident_id)) {
        announced.push(notification("diagnosis", item));
      }
    }
  }

  return {
    notifications: announced.slice(0, MAX_BURST),
    overflow: Math.max(0, announced.length - MAX_BURST),
    announcedIncidentIds: [...new Set(announced.map((notice) => notice.incidentId))],
    seen: {
      incidents: remember(
        previous?.incidents ?? [],
        [...ordered].reverse().map((item) => item.incident_id).filter((id) => !knownIncidents.has(id)),
      ),
      diagnosed: remember(
        previous?.diagnosed ?? [],
        [...ordered]
          .reverse()
          .filter((item) => item.has_diagnosis && !knownDiagnosed.has(item.incident_id))
          .map((item) => item.incident_id),
      ),
    },
  };
}
