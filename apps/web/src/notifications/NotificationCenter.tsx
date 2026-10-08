import { useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { Link } from "react-router-dom";
import { Bell, CircleAlert, ScanLine, X, CheckCheck } from "lucide-react";
import { useNotifications } from "./context";
import { ScrollArea } from "@/components/ui/ScrollArea";
import { dateTime } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";
import { severityTone } from "@/lib/tones";

export function NotificationCenter() {
  const { history, unreadCount, unreadIds, markRead, markAllRead } =
    useNotifications();
  const [open, setOpen] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const visible = unreadOnly
    ? history.filter((record) => unreadIds.has(record.notice.incidentId))
    : history;
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label={`Notifications${unreadCount ? `, ${unreadCount} unread incidents` : ""}`}
          title="Notifications"
          className="relative rounded-md p-2 text-muted hover:bg-surface-raised hover:text-text"
        >
          <Bell size={18} aria-hidden />
          {unreadCount > 0 && (
            <span className="absolute -right-1 -top-1 rounded bg-accent px-1 text-[9px] font-medium text-accent-fg">
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          collisionPadding={12}
          aria-label="Notification center"
          className="z-50 flex w-[420px] max-w-[calc(100vw-24px)] max-h-[var(--radix-popover-content-available-height)] flex-col overflow-hidden rounded-lg border border-border-strong bg-surface-raised text-text shadow-md"
        >
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <h2 className="text-sm font-semibold">Notifications</h2>
            <Popover.Close
              aria-label="Close notifications"
              className="rounded p-1 text-subtle hover:bg-surface"
            >
              <X size={16} aria-hidden />
            </Popover.Close>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2">
            <button
              type="button"
              aria-pressed={unreadOnly}
              onClick={() => setUnreadOnly(!unreadOnly)}
              className={`rounded px-2 py-1 text-xs ${unreadOnly ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface"}`}
            >
              Unread only
            </button>
            <button
              type="button"
              disabled={unreadCount === 0}
              onClick={markAllRead}
              className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-accent disabled:text-subtle disabled:opacity-50"
            >
              <CheckCheck size={13} aria-hidden />
              Mark all read
            </button>
          </div>
          <ScrollArea className="h-[min(380px,55dvh)] min-h-0 shrink">
            {visible.length ? (
              <ul className="divide-y divide-border">
                {visible.map(({ notice, observedAt }) => {
                  const unread = unreadIds.has(notice.incidentId);
                  const Icon =
                    notice.kind === "diagnosis" ? ScanLine : CircleAlert;
                  return (
                    <li key={notice.id} className="flex gap-3 p-4">
                      <Icon
                        size={16}
                        aria-hidden
                        className={`mt-0.5 shrink-0 ${unread ? "text-accent" : "text-subtle"}`}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-[11px] text-subtle">
                            {notice.kind === "diagnosis"
                              ? "Diagnosis ready"
                              : "New incident"}
                          </span>
                          {notice.kind === "incident" && (
                            <Badge
                              tone={severityTone(notice.severity)}
                              className="text-[10px]"
                            >
                              {notice.severity}
                            </Badge>
                          )}
                          {unread && (
                            <span
                              className="h-1.5 w-1.5 rounded-full bg-accent"
                              aria-label="Unread"
                            />
                          )}
                        </div>
                        <Link
                          to={`/incidents/${encodeURIComponent(notice.incidentId)}`}
                          onClick={() => {
                            markRead(notice.incidentId);
                            setOpen(false);
                          }}
                          className="mt-1 block break-anywhere text-sm font-medium hover:text-accent"
                        >
                          {notice.title}
                        </Link>
                        {notice.service && (
                          <p className="mt-1 break-anywhere font-mono text-[11px] text-muted">
                            {notice.service}
                          </p>
                        )}
                        {(notice.diagnosed || notice.kind === "diagnosis") && (
                          <p className="mt-1 break-anywhere text-[11px] text-muted">
                            Confidence {notice.confidence ?? "not recorded"} ·
                            Resolution {notice.resolution ?? "not recorded"}
                          </p>
                        )}
                        <time
                          dateTime={observedAt}
                          title={`Observed by this browser: ${observedAt}`}
                          className="mt-2 block text-[10px] text-subtle"
                        >
                          Observed {dateTime(observedAt)}
                        </time>
                      </div>
                      {unread && (
                        <button
                          type="button"
                          aria-label={`Mark ${notice.title} read`}
                          title="Mark incident read"
                          onClick={() => markRead(notice.incidentId)}
                          className="self-start rounded p-1 text-subtle hover:text-accent"
                        >
                          <CheckCheck size={14} aria-hidden />
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <div className="p-6 text-center">
                <Bell
                  size={20}
                  className="mx-auto mb-3 text-subtle"
                  aria-hidden
                />
                <p className="text-sm font-medium">
                  {unreadOnly
                    ? "No unread notifications."
                    : "No notifications recorded yet."}
                </p>
                <p className="mt-2 text-xs text-muted">
                  New incidents and first stored diagnoses appear here as this
                  browser observes them.
                </p>
              </div>
            )}
          </ScrollArea>
          <div className="border-t border-border px-4 py-3">
            <p className="mb-2 text-[10px] text-subtle">
              This browser · latest 100 notices · unread count is per incident.
              Recent incidents and first diagnoses only; not a complete activity
              log.
            </p>
            <Link
              to="/incidents"
              onClick={() => setOpen(false)}
              className="text-xs font-medium text-accent hover:underline"
            >
              View all incidents →
            </Link>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
