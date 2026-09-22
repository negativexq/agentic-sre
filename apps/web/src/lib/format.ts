/** Compact humanized duration, e.g. 42 → "42s", 180 → "3.0m", 7200 → "2.0h". */
export function humanizeSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 0) return "—";
  if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${minutes.toFixed(1)}m`;
  return `${(minutes / 60).toFixed(1)}h`;
}

/** Relative age from an ISO timestamp, e.g. "3m", "2h", "4d". */
export function ageFrom(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.max((Date.now() - then) / 1000, 0);
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86_400)}d`;
}

/** Local wall-clock time, seconds precision. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Local date + time. */
export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Shorten a canonical namespace/Kind/name for dense table cells. */
export function shortEntity(entity: string | null | undefined): string {
  if (!entity) return "—";
  const parts = entity.split("/");
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : entity;
}
