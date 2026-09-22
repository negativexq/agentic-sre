import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export type TimelineItem = {
  label: ReactNode;
  detail?: ReactNode;
  at?: ReactNode;
  tone?: "default" | "accent" | "critical";
};

const DOT: Record<NonNullable<TimelineItem["tone"]>, string> = {
  default: "bg-border-strong",
  accent: "bg-accent",
  critical: "bg-critical",
};

export function Timeline({ items }: { items: TimelineItem[] }) {
  return (
    <ol className="relative ml-2 space-y-4 border-l border-border pl-5">
      {items.map((item, index) => (
        <li key={index} className="relative">
          <span
            className={cn(
              "absolute -left-[26px] top-1 h-3 w-3 rounded-full ring-4 ring-surface-raised",
              DOT[item.tone ?? "default"],
            )}
          />
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
            <span className="text-sm font-medium text-text break-anywhere">{item.label}</span>
            {item.at && <span className="font-mono text-xs text-subtle">{item.at}</span>}
          </div>
          {item.detail && (
            <p className="mt-0.5 text-sm text-muted break-anywhere">{item.detail}</p>
          )}
        </li>
      ))}
    </ol>
  );
}
