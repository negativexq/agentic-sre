import type { ReactNode } from "react";

import { cn } from "@/lib/cn";
import * as Popover from "@radix-ui/react-popover";
import { CircleHelp, X } from "lucide-react";

const EXPLANATIONS: Record<string, string> = {
  VERIFIED:
    "A deterministic rule verifies the actor's link to the symptoms. Confidence is separate from resolution: alternatives can still remain.",
  LIKELY:
    "The engine reports qualified support for the actor's link to the symptoms. This is not a probability or a resolved diagnosis.",
  UNVERIFIED:
    "The actor's link to the symptoms has not been verified by a deterministic rule. This does not mean the actor has been disproved.",
  RESOLVED:
    "The engine reports that the evidence distinguishes the causal conclusion from the leading alternatives. This diagnosis state does not mean the incident has recovered.",
  AMBIGUOUS:
    "The evidence leaves causal distinctions open. This is an uncertainty state, not a diagnosis error.",
  INSUFFICIENT_EVIDENCE:
    "The available evidence is insufficient to establish the causal conclusion. This is an evidence limit, not a failed investigation.",
};

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
  const explanation =
    typeof children === "string" ? EXPLANATIONS[children] : undefined;
  const style = cn(
    "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium tracking-wide",
    TONES[tone],
    className,
  );
  if (explanation)
    return (
      <Popover.Root>
        <Popover.Trigger asChild>
          <button
            type="button"
            aria-label={`${children}: explain status`}
            onClick={(event) => event.stopPropagation()}
            className={style}
          >
            {children}
            <CircleHelp size={11} aria-hidden />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            aria-label={`${children} explanation`}
            sideOffset={6}
            collisionPadding={12}
            className="z-50 w-80 max-w-[calc(100vw-24px)] rounded-lg border border-border-strong bg-surface-raised p-4 text-text shadow-md"
          >
            <div className="mb-2 flex items-center justify-between gap-3">
              <span className="text-xs font-semibold">{children}</span>
              <Popover.Close
                aria-label="Close status explanation"
                className="rounded p-1 text-subtle hover:bg-surface"
              >
                <X size={14} aria-hidden />
              </Popover.Close>
            </div>
            <p className="text-sm text-muted">{explanation}</p>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    );
  return <span className={style}>{children}</span>;
}
