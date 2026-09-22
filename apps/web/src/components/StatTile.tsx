import type { ReactNode } from "react";

import { Card } from "@/components/ui/Card";
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
    <Card className="px-4 py-3">
      <p className="text-xs font-medium uppercase tracking-wide text-subtle">{label}</p>
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
    </Card>
  );
}
