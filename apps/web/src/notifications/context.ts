import { createContext, useContext } from "react";

import type { AppNotification } from "@/notifications/diff";
import type { NotificationRecord } from "@/notifications/storage";

/** One toast: a single notification, or a summary of further ones that arrived in the same burst. */
export interface ToastItem {
  key: string;
  notice: AppNotification | null;
  more: number;
}

export interface NotificationContextValue {
  toasts: ToastItem[];
  dismiss: (key: string) => void;
  /** Announcements not yet looked at; cleared by visiting the incident list or the incident itself. */
  unreadCount: number;
  history: NotificationRecord[];
  unreadIds: ReadonlySet<string>;
  markRead: (incidentId: string) => void;
  markAllRead: () => void;
}

export const NotificationContext = createContext<NotificationContextValue>({
  toasts: [],
  dismiss: () => undefined,
  unreadCount: 0,
  history: [],
  unreadIds: new Set(),
  markRead: () => undefined,
  markAllRead: () => undefined,
});

export function useNotifications(): NotificationContextValue {
  return useContext(NotificationContext);
}
