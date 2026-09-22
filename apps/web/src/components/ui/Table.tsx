import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className="overflow-x-auto">
      <table className={cn("w-full border-collapse text-sm", className)}>{children}</table>
    </div>
  );
}

export function THead({ columns }: { columns: ReactNode[] }) {
  return (
    <thead>
      <tr className="border-b border-border text-left text-xs font-medium uppercase tracking-wide text-subtle">
        {columns.map((column, index) => (
          <th key={index} className="px-3 py-2 font-medium">
            {column}
          </th>
        ))}
      </tr>
    </thead>
  );
}

export function TRow({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick?: () => void;
}) {
  return (
    <tr
      onClick={onClick}
      className={cn(
        "border-b border-border/60 last:border-0",
        onClick && "cursor-pointer transition-colors hover:bg-surface",
      )}
    >
      {children}
    </tr>
  );
}

export function TCell({ children, className }: { children: ReactNode; className?: string }) {
  return <td className={cn("px-3 py-2.5 align-middle", className)}>{children}</td>;
}
