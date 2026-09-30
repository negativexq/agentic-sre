import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/cn";
import { shortEntity } from "@/lib/format";
import { confidenceTone, resolutionTone, severityTone } from "@/lib/tones";
import type { AppNotification } from "@/notifications/diff";
import { useNotifications, type ToastItem } from "@/notifications/context";

const AUTO_DISMISS_MS = 12_000;

function label(value: string | null): string {
  if (!value) return "";
  const words = value.replace(/_/g, " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const EDGE: Record<string, string> = {
  CRITICAL: "border-l-critical",
  WARNING: "border-l-warning",
};

function Notice({ notice }: { notice: AppNotification }) {
  const isDiagnosis = notice.kind === "diagnosis";
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-text">
          {isDiagnosis ? "Diagnosis ready" : "New incident"}
        </span>
        {!isDiagnosis ? <Badge tone={severityTone(notice.severity)}>{label(notice.severity)}</Badge> : null}
      </div>
      <p className="mt-1 break-words text-sm text-text">{notice.title}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
        {notice.service ? <span>{notice.service}</span> : null}
        {notice.diagnosed || isDiagnosis ? (
          <>
            {notice.resolution ? (
              <Badge tone={resolutionTone(notice.resolution)}>{label(notice.resolution)}</Badge>
            ) : null}
            {notice.confidence ? (
              <Badge tone={confidenceTone(notice.confidence)}>{label(notice.confidence)}</Badge>
            ) : null}
            {notice.leadingActor ? <span>{shortEntity(notice.leadingActor)}</span> : null}
          </>
        ) : null}
      </div>
    </>
  );
}

function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: (key: string) => void }) {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    if (paused) return;
    const timer = window.setTimeout(() => onDismiss(toast.key), AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [paused, toast.key, onDismiss]);

  const target = toast.notice ? `/incidents/${toast.notice.incidentId}` : "/incidents";
  return (
    <li
      role="status"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      className={cn(
        "pointer-events-auto rounded-lg border border-border border-l-4 bg-surface-raised p-3 shadow-lg",
        toast.notice ? (EDGE[toast.notice.severity] ?? "border-l-accent") : "border-l-accent",
        toast.notice?.kind === "diagnosis" && "border-l-accent",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          {toast.notice ? (
            <Notice notice={toast.notice} />
          ) : (
            <p className="text-sm font-semibold text-text">
              {toast.more} more {toast.more === 1 ? "notification" : "notifications"}
            </p>
          )}
          <Link
            to={target}
            onClick={() => onDismiss(toast.key)}
            className="mt-2 inline-block text-xs font-medium text-accent hover:underline"
          >
            {toast.notice ? "Open incident" : "View incidents"}
          </Link>
        </div>
        <button
          type="button"
          onClick={() => onDismiss(toast.key)}
          aria-label="Dismiss notification"
          className="rounded-md px-1.5 text-lg leading-none text-subtle hover:bg-surface hover:text-text"
        >
          ×
        </button>
      </div>
    </li>
  );
}

/** Fixed stack of toasts. An always-present polite live region, so a screen reader hears each addition. */
export function NotificationViewport() {
  const { toasts, dismiss } = useNotifications();
  return (
    <ol
      aria-live="polite"
      aria-relevant="additions"
      aria-label="Notifications"
      className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2"
    >
      {toasts.map((toast) => (
        <ToastCard key={toast.key} toast={toast} onDismiss={dismiss} />
      ))}
    </ol>
  );
}
