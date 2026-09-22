import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export type BadgeTone =
  | "neutral"
  | "accent"
  | "critical"
  | "warning"
  | "healthy"
  | "verified"
  | "likely"
  | "unverified";

const TONES: Record<BadgeTone, string> = {
  neutral: "bg-unverified-soft text-muted",
  accent: "bg-accent-soft text-accent",
  critical: "bg-critical-soft text-critical",
  warning: "bg-warning-soft text-warning",
  healthy: "bg-healthy-soft text-healthy",
  verified: "bg-verified-soft text-verified",
  likely: "bg-likely-soft text-likely",
  unverified: "bg-unverified-soft text-muted",
};

export function Badge({
  tone = "neutral",
  children,
  className,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium tracking-wide",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
