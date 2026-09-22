import type { ReactNode } from "react";
import { useEffect } from "react";

import { cn } from "@/lib/cn";

/** A right-side sheet for previews and detail drill-downs. */
export function Drawer({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <div className={cn("fixed inset-0 z-50", open ? "" : "pointer-events-none")} aria-hidden={!open}>
      <div
        onClick={onClose}
        className={cn(
          "absolute inset-0 bg-black/40 transition-opacity",
          open ? "opacity-100" : "opacity-0",
        )}
      />
      <div
        role="dialog"
        aria-modal="true"
        className={cn(
          "absolute right-0 top-0 flex h-full w-full max-w-2xl flex-col border-l border-border bg-surface-raised shadow-xl transition-transform",
          open ? "translate-x-0" : "translate-x-full",
        )}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold text-text">{title}</h2>
          <button
            onClick={onClose}
            className="rounded-md px-2 py-1 text-sm text-muted hover:bg-surface hover:text-text"
          >
            Close
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  );
}
