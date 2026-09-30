import type { SeenState } from "@/notifications/diff";

const KEY = "agentic-sre.notifications.v1";

function isIdList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
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
    return isIdList(incidents) && isIdList(diagnosed) ? { incidents, diagnosed } : null;
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
