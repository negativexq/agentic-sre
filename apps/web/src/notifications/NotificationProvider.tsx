import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";

import { api } from "@/api/client";
import { useLiveUpdates } from "@/api/useLiveUpdates";
import { NotificationContext, type ToastItem } from "@/notifications/context";
import { diffIncidents, type SeenState } from "@/notifications/diff";
import {
  loadSeen,
  saveSeen,
  loadCenter,
  saveCenter,
  HISTORY_LIMIT,
} from "@/notifications/storage";

const MAX_TOASTS = 4;
const WATCH_LIMIT = 50;
const INCIDENT_ROUTE = /^\/incidents\/([^/]+)$/;

/**
 * Turns persisted incident state into operator notifications. The console already refetches on every
 * persisted-state signal (SSE); this watches the same incident list and announces what is new in it.
 */
export function NotificationProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [initialCenter] = useState(loadCenter);
  const [history, setHistory] = useState(initialCenter.history);
  const [unreadIds, setUnreadIds] = useState<ReadonlySet<string>>(
    new Set(initialCenter.unreadIds),
  );
  const viewing = useRef<{ list: boolean; incident: string | null }>({
    list: false,
    incident: null,
  });
  const seen = useRef<SeenState | null | undefined>(undefined);
  const baseTitle = useRef<string>(
    typeof document === "undefined" ? "" : document.title,
  );
  const location = useLocation();

  useLiveUpdates("/stream", [["incident-watch"]]);
  const watch = useQuery({
    queryKey: ["incident-watch"],
    queryFn: () => api.incidents({ limit: WATCH_LIMIT }),
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (!watch.data) return;
    if (seen.current === undefined) seen.current = loadSeen();
    const result = diffIncidents(seen.current, watch.data.items);
    seen.current = result.seen;
    saveSeen(result.seen);
    if (result.notifications.length === 0 && result.overflow === 0) return;
    const observedAt = new Date().toISOString();
    setHistory((current) => {
      const added = new Set(result.allNotifications.map((notice) => notice.id));
      return [
        ...result.allNotifications.map((notice) => ({ notice, observedAt })),
        ...current.filter((record) => !added.has(record.notice.id)),
      ].slice(0, HISTORY_LIMIT);
    });
    setToasts((current) =>
      [
        ...result.notifications.map((notice) => ({
          key: notice.id,
          notice,
          more: 0,
        })),
        ...(result.overflow > 0
          ? [{ key: `more:${Date.now()}`, notice: null, more: result.overflow }]
          : []),
        ...current,
      ].slice(0, MAX_TOASTS),
    );
    // What is on screen right now is not "unread": the list shows every incident, and an open incident
    // shows itself. Everything announced elsewhere is counted once, by incident id.
    const onScreen = document.visibilityState === "visible";
    setUnreadIds((current) => {
      const next = new Set(current);
      for (const id of result.announcedIncidentIds) {
        if (
          onScreen &&
          (viewing.current.list || viewing.current.incident === id)
        )
          continue;
        next.add(id);
      }
      return next;
    });
  }, [watch.data]);

  useEffect(() => {
    const path = location.pathname.replace(/\/$/, "") || "/";
    const match = INCIDENT_ROUTE.exec(path);
    viewing.current = {
      list: path === "/incidents",
      incident: match ? match[1] : null,
    };
    if (path === "/incidents") {
      setUnreadIds(new Set());
      return;
    }
    if (match) {
      const id = match[1];
      setUnreadIds((current) => {
        if (!current.has(id)) return current;
        const next = new Set(current);
        next.delete(id);
        return next;
      });
    }
  }, [location.pathname]);

  const unreadCount = unreadIds.size;
  useEffect(() => {
    document.title =
      unreadCount > 0
        ? `(${unreadCount}) ${baseTitle.current}`
        : baseTitle.current;
  }, [unreadCount]);

  const dismiss = useCallback((key: string) => {
    setToasts((current) => current.filter((toast) => toast.key !== key));
  }, []);

  const markRead = useCallback((id: string) => {
    setUnreadIds((current) => {
      const next = new Set(current);
      next.delete(id);
      return next;
    });
  }, []);
  const markAllRead = useCallback(() => setUnreadIds(new Set()), []);
  useEffect(() => {
    saveCenter({ history, unreadIds: [...unreadIds] });
  }, [history, unreadIds]);

  const value = useMemo(
    () => ({
      toasts,
      dismiss,
      unreadCount,
      history,
      unreadIds,
      markRead,
      markAllRead,
    }),
    [toasts, dismiss, unreadCount, history, unreadIds, markRead, markAllRead],
  );
  return (
    <NotificationContext.Provider value={value}>
      {children}
    </NotificationContext.Provider>
  );
}
