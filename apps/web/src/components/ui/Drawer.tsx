// shadcn/ui Sheet composition, adapted to the console's existing Drawer API.
import { type ReactNode, useRef } from "react";
import * as Sheet from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { ScrollArea } from "./ScrollArea";

export function Drawer({
  open,
  onClose,
  title,
  children,
  onAfterClose,
  restoreFocusLabel,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  onAfterClose?: () => void;
  /** A logical replacement trigger when a selected graph moves into the shared canvas. */
  restoreFocusLabel?: string | null;
}) {
  const trigger = useRef<HTMLElement | null>(null);
  return (
    <Sheet.Root
      open={open}
      onOpenChange={(value) => {
        if (!value) onClose();
      }}
    >
      <Sheet.Portal>
        <Sheet.Overlay className="fixed inset-0 z-50 bg-[rgb(4_9_16_/_60%)]" />
        <Sheet.Content
          aria-describedby={undefined}
          className="fixed inset-y-0 right-0 z-50 flex h-dvh w-full max-w-[520px] flex-col border-l border-border bg-surface-raised text-text outline-none"
          onOpenAutoFocus={() => {
            trigger.current = document.activeElement as HTMLElement | null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (
              trigger.current?.isConnected &&
              trigger.current !== document.body &&
              trigger.current.getClientRects().length
            )
              trigger.current.focus();
            else if (restoreFocusLabel) {
              const replacement = [
                ...document.querySelectorAll<HTMLElement>("button[aria-label]"),
              ].find(
                (element) =>
                  element.getAttribute("aria-label") === restoreFocusLabel &&
                  element.getClientRects().length > 0,
              );
              replacement?.focus();
            }
            onAfterClose?.();
          }}
        >
          <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-5 py-4">
            <Sheet.Title className="text-sm font-semibold">{title}</Sheet.Title>
            <Sheet.Close
              aria-label="Close inspector"
              className="rounded-md p-2 text-muted hover:bg-surface"
            >
              <X size={18} aria-hidden />
            </Sheet.Close>
          </div>
          <ScrollArea className="flex-1">
            <div className="p-5">{children}</div>
          </ScrollArea>
        </Sheet.Content>
      </Sheet.Portal>
    </Sheet.Root>
  );
}
