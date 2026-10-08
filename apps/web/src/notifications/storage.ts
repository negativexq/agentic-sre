import type { SeenState, AppNotification } from "@/notifications/diff";

export interface NotificationRecord {
  notice: AppNotification;
  observedAt: string;
}
export interface NotificationCenterState {
  history: NotificationRecord[];
  unreadIds: string[];
}
export const HISTORY_LIMIT = 100;
const CENTER_KEY = "agentic-sre.notification-center.v1";

export function loadCenter(): NotificationCenterState {
  const empty = { history: [], unreadIds: [] };
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(CENTER_KEY) ?? "null",
    );
    if (
      !parsed ||
      !Array.isArray(parsed.history) ||
      !isIdList(parsed.unreadIds)
    )
      return empty;
    const history: NotificationRecord[] = parsed.history
      .filter((record: NotificationRecord) => {
        const n = record?.notice;
        return (
          n &&
          ["incident", "diagnosis"].includes(n.kind) &&
          [n.id, n.incidentId, n.title, n.severity].every(
            (value) => typeof value === "string",
          ) &&
          [n.service, n.resolution, n.leadingActor, n.confidence].every(
            (value) => value === null || typeof value === "string",
          ) &&
          typeof n.diagnosed === "boolean" &&
          typeof record.observedAt === "string" &&
          Number.isFinite(Date.parse(record.observedAt))
        );
      })
      .slice(0, HISTORY_LIMIT);
    const ids = new Set(history.map((record) => record.notice.incidentId));
    return {
      history,
      unreadIds: parsed.unreadIds.filter((id: string) => ids.has(id)),
    };
  } catch {
    return empty;
  }
}

export function saveCenter(state: NotificationCenterState): void {
  try {
    window.localStorage.setItem(CENTER_KEY, JSON.stringify(state));
  } catch {
    /* unavailable storage: session-only center */
  }
}

const KEY = "agentic-sre.notifications.v1";

function isIdList(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}

/**
 * What this browser has already been told about, or `null` on the first visit. Storage can be missing,
 * blocked or corrupted (private windows, cleared site data); every failure reads as "first visit", which
 * announces nothing rather than everything.
 */
export function loadSeen(): SeenState | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const { incidents, diagnosed } = parsed as Record<string, unknown>;
    return isIdList(incidents) && isIdList(diagnosed)
      ? { incidents, diagnosed }
      : null;
  } catch {
    return null;
  }
}

export function saveSeen(state: SeenState): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    // Not persisted; the next visit starts from a silent baseline.
  }
}
