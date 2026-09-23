import { Suspense } from "react";
import { NavLink, Outlet } from "react-router-dom";

import { Skeleton } from "@/components/ui/States";
import { cn } from "@/lib/cn";
import { useTheme } from "@/lib/theme";

const PRIMARY = [
  { to: "/", label: "Overview", end: true },
  { to: "/incidents", label: "Incidents" },
  { to: "/changes", label: "Changes" },
  { to: "/reports", label: "Reports" },
];

const SECONDARY = [
  { to: "/connections", label: "Connections" },
  { to: "/settings", label: "Settings" },
];

function NavItem({ to, label, end }: { to: string; label: string; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn(
          "block rounded-lg px-3 py-2 text-sm font-medium transition-colors",
          isActive ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface hover:text-text",
        )
      }
    >
      {label}
    </NavLink>
  );
}

export function Layout() {
  const { theme, toggle } = useTheme();

  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-surface px-3 py-4 md:flex">
        <div className="px-3 pb-5">
          <p className="text-sm font-semibold tracking-tight text-text">Agentic SRE</p>
          <p className="text-xs text-subtle">Operator Console</p>
        </div>
        <nav className="flex flex-1 flex-col gap-1">
          {PRIMARY.map((item) => (
            <NavItem key={item.to} {...item} />
          ))}
          <div className="my-2 border-t border-border" />
          {SECONDARY.map((item) => (
            <NavItem key={item.to} {...item} />
          ))}
        </nav>
        <button
          onClick={toggle}
          className="mt-2 rounded-lg px-3 py-2 text-left text-sm text-muted hover:bg-surface-raised hover:text-text"
        >
          {theme === "dark" ? "Light mode" : "Dark mode"}
        </button>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Mobile top nav */}
        <header className="flex items-center gap-2 overflow-x-auto border-b border-border bg-surface px-4 py-2 md:hidden">
          <span className="mr-2 text-sm font-semibold">Agentic SRE</span>
          {[...PRIMARY, ...SECONDARY].map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  "whitespace-nowrap rounded-md px-2 py-1 text-sm",
                  isActive ? "bg-accent-soft text-accent" : "text-muted",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </header>
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6">
          <Suspense fallback={<Skeleton className="h-48" />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
