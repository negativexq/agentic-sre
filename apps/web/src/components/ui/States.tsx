import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/** Skeleton shimmer block for loading states. */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-border/60", className)} />;
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border px-6 py-12 text-center">
      <p className="text-sm font-medium text-text">{title}</p>
      {description && <p className="max-w-sm text-sm text-muted">{description}</p>}
      {action}
    </div>
  );
}

type AlertTone = "info" | "warning" | "critical";

const ALERT_TONES: Record<AlertTone, string> = {
  info: "border-accent/30 bg-accent-soft text-text",
  warning: "border-warning/30 bg-warning-soft text-text",
  critical: "border-critical/30 bg-critical-soft text-text",
};

export function Alert({
  tone = "info",
  children,
}: {
  tone?: AlertTone;
  children: ReactNode;
}) {
  return (
    <div className={cn("rounded-lg border px-3 py-2 text-sm", ALERT_TONES[tone])}>{children}</div>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <Alert tone="critical">
      <span className="font-medium">Could not load.</span> {message}
    </Alert>
  );
}
