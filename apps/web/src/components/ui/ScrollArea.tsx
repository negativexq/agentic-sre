// Adapted from shadcn/ui's Radix ScrollArea composition (MIT).
import type { ReactNode } from "react";
import * as Primitive from "@radix-ui/react-scroll-area";
import { cn } from "@/lib/cn";
export function ScrollArea({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <Primitive.Root
      className={cn("relative min-h-0 overflow-hidden", className)}
    >
      <Primitive.Viewport className="h-full w-full rounded-[inherit] [&>div]:!block">
        {children}
      </Primitive.Viewport>
      <Primitive.Scrollbar
        orientation="vertical"
        className="flex w-2.5 touch-none select-none p-0.5"
      >
        <Primitive.Thumb className="relative flex-1 rounded-full bg-border-strong" />
      </Primitive.Scrollbar>
      <Primitive.Corner />
    </Primitive.Root>
  );
}
