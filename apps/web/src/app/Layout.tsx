import { Suspense, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  LayoutDashboard,
  CircleAlert,
  GitCompareArrows,
  FileText,
  Plug,
  Settings2,
  Sun,
  Moon,
  Menu,
  PanelLeft,
  ChevronRight,
  type LucideIcon,
} from "lucide-react";
import { Drawer } from "@/components/ui/Drawer";
import { Skeleton } from "@/components/ui/States";
import { cn } from "@/lib/cn";
import { useTheme } from "@/lib/theme";
import { useNotifications } from "@/notifications/context";
import { NotificationProvider } from "@/notifications/NotificationProvider";
import { NotificationViewport } from "@/notifications/NotificationViewport";
import { NotificationCenter } from "@/notifications/NotificationCenter";

const PRIMARY = [
  { to: "/", label: "Overview", icon: LayoutDashboard },
  { to: "/incidents", label: "Incidents", icon: CircleAlert },
  { to: "/changes", label: "Changes", icon: GitCompareArrows },
  { to: "/reports", label: "Reports", icon: FileText },
];
const SECONDARY = [
  { to: "/connections", label: "Connections", icon: Plug },
  { to: "/settings", label: "Settings", icon: Settings2 },
];
function NavItem({
  to,
  label,
  icon: Icon,
  count = 0,
  onNavigate,
  collapsed = false,
}: {
  to: string;
  label: string;
  icon: LucideIcon;
  count?: number;
  onNavigate?: () => void;
  collapsed?: boolean;
}) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      title={label}
      aria-label={collapsed ? label : undefined}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          collapsed
            ? "relative flex justify-center rounded-md p-2"
            : "flex items-center gap-3 rounded-md border px-3 py-2 text-[13px] font-medium transition-colors",
          isActive
            ? "border-accent/20 bg-accent-soft text-accent"
            : "border-transparent text-muted hover:bg-surface-raised hover:text-text",
        )
      }
    >
      <Icon size={17} strokeWidth={1.7} aria-hidden />
      {!collapsed && label}
      {collapsed && count > 0 && (
        <span
          aria-label={`${count} unread`}
          className="absolute -right-1 -top-1 rounded bg-accent px-1 text-[9px] text-accent-fg"
        >
          {count > 99 ? "99+" : count}
        </span>
      )}
      {!collapsed && count > 0 && (
        <span
          aria-label={`${count} unread`}
          className="ml-auto rounded bg-accent px-1.5 text-xs text-accent-fg"
        >
          {count > 99 ? "99+" : count}
        </span>
      )}
    </NavLink>
  );
}
function LayoutFrame() {
  const { theme, toggle } = useTheme();
  const { unreadCount } = useNotifications();
  const [menu, setMenu] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const compact = collapsed && !menu;
  const location = useLocation();
  const section =
    [...PRIMARY, ...SECONDARY].find((item) =>
      item.to === "/"
        ? location.pathname === "/"
        : location.pathname.startsWith(item.to),
    )?.label ?? "Console";
  const navigation = (
    <>
      <div
        className={cn(
          "flex items-center gap-3 pb-7 pt-2",
          compact ? "justify-center" : "px-3",
        )}
      >
        <div className="shrink-0 rounded-lg border border-accent/30 bg-accent-soft p-2 text-accent">
          <Activity size={21} aria-hidden />
        </div>
        <div className={compact ? "hidden" : ""}>
          <p className="text-sm font-semibold tracking-tight">Agentic SRE</p>
          <p className="text-[11px] text-subtle">Operator Console</p>
        </div>
      </div>
      <p
        className={cn(
          compact && "sr-only",
          "px-3 pb-2 text-[10px] font-medium uppercase tracking-[.14em] text-subtle",
        )}
      >
        Operations
      </p>
      <nav aria-label="Primary navigation" className="flex flex-col gap-1">
        {PRIMARY.map((item) => (
          <NavItem
            key={item.to}
            {...item}
            count={item.to === "/incidents" ? unreadCount : 0}
            collapsed={compact}
            onNavigate={() => setMenu(false)}
          />
        ))}
        <div className="my-4 border-t border-border" />
        {SECONDARY.map((item) => (
          <NavItem
            key={item.to}
            {...item}
            collapsed={compact}
            onNavigate={() => setMenu(false)}
          />
        ))}
      </nav>
      <div className="mt-auto border-t border-border pt-4">
        <p className={cn("px-3 text-[11px] text-subtle", compact && "hidden")}>
          Evidence-driven · Read-only investigation
        </p>
        <button
          aria-label={theme === "dark" ? "Light theme" : "Dark theme"}
          title={theme === "dark" ? "Light theme" : "Dark theme"}
          onClick={toggle}
          className="mt-2 flex w-full items-center gap-3 rounded-md px-3 py-2 text-xs text-muted hover:bg-surface-raised"
        >
          {theme === "dark" ? (
            <Sun size={16} aria-hidden />
          ) : (
            <Moon size={16} aria-hidden />
          )}
          {!compact && (theme === "dark" ? "Light theme" : "Dark theme")}
        </button>
      </div>
    </>
  );
  return (
    <div className="flex min-h-screen">
      <a
        href="#main-content"
        className="fixed left-4 top-4 z-50 -translate-y-24 rounded bg-accent p-3 text-accent-fg focus:translate-y-0"
      >
        Skip to content
      </a>
      <aside
        className={cn(
          "sticky top-0 hidden h-screen shrink-0 flex-col bg-bg px-3 py-4 lg:flex",
          collapsed ? "w-[72px]" : "w-[232px]",
        )}
      >
        {navigation}
      </aside>
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-bg lg:my-2 lg:mr-2 lg:rounded-lg lg:border lg:border-border">
        <header className="flex h-12 shrink-0 items-center gap-3 border-b border-border bg-surface px-4 sm:px-7">
          <button
            onClick={() => setMenu(true)}
            aria-label="Open navigation"
            className="rounded p-1 text-muted lg:hidden"
          >
            <Menu size={20} />
          </button>
          <button
            aria-label="Toggle sidebar"
            aria-expanded={!collapsed}
            onClick={() => setCollapsed(!collapsed)}
            className="hidden rounded-md p-1 text-muted hover:bg-surface-raised lg:block"
          >
            <PanelLeft size={18} aria-hidden />
          </button>
          <span className="text-xs text-subtle">Console</span>
          <ChevronRight size={12} className="text-subtle" aria-hidden />
          <span className="text-xs text-text">{section}</span>
          <div className="ml-auto flex items-center gap-4">
            <span className="hidden text-[11px] text-subtle sm:block">
              Deterministic RCA
            </span>
            <NotificationCenter />
          </div>
        </header>
        <main
          id="main-content"
          tabIndex={-1}
          className="mx-auto w-full max-w-[1800px] min-w-0 flex-1 px-4 py-6 sm:px-7 lg:py-7"
        >
          <Suspense fallback={<Skeleton className="h-48" />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
      <Drawer open={menu} onClose={() => setMenu(false)} title="Navigation">
        <div className="flex min-h-full flex-col">{navigation}</div>
      </Drawer>
      <NotificationViewport />
    </div>
  );
}
export function Layout() {
  return (
    <NotificationProvider>
      <LayoutFrame />
    </NotificationProvider>
  );
}
