import { cn } from "@/lib/cn";
import type { LiveState } from "@/api/useLiveUpdates";

const LABEL: Record<LiveState, string> = {
  connecting: "Connecting…",
  live: "Live",
  offline: "Polling",
};

const DOT: Record<LiveState, string> = {
  connecting: "bg-warning animate-pulse",
  live: "bg-healthy",
  offline: "bg-border-strong",
};

export function LiveBadge({ state }: { state: LiveState }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted">
      <span className={cn("h-2 w-2 rounded-full", DOT[state])} />
      {LABEL[state]}
    </span>
  );
}
