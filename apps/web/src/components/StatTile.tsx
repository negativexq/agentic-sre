import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export function StatTile({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: "default" | "critical" | "accent";
}) {
  return (
    <div className="min-w-0 border-r border-border px-5 py-4 last:border-0">
      <p className="text-xs font-medium uppercase tracking-wide text-subtle">
        {label}
      </p>
      <p
        className={cn(
          "mt-1 text-2xl font-semibold tabular-nums",
          tone === "critical" && "text-critical",
          tone === "accent" && "text-accent",
          tone === "default" && "text-text",
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </div>
  );
}
