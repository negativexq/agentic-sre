import { Database, Plug } from "lucide-react";
import { cn } from "@/lib/cn";
import type {
  ConnectorStatus,
  SystemConnector,
  SystemStatus,
} from "@/api/types";
const DOT: Record<ConnectorStatus, string> = {
  connected: "bg-healthy",
  degraded: "bg-warning",
  unavailable: "bg-critical",
  not_configured: "bg-border-strong",
};
/** The DTO's detail distinguishes a configuration/capability from a health probe. */
function presentation(connector: SystemConnector) {
  if (connector.status !== "connected")
    return {
      label:
        connector.status === "not_configured"
          ? "Not configured"
          : connector.status === "degraded"
            ? "Degraded"
            : "Unavailable",
      dot: DOT[connector.status],
    };
  if (
    connector.detail === "configured; not actively probed" ||
    connector.detail === "cluster reader configured"
  )
    return { label: "Configured · unverified", dot: "bg-border-strong" };
  if (connector.detail === "webhook receiver ready")
    return { label: "Receiver ready", dot: "bg-accent" };
  if (connector.detail === "read through the connector")
    return { label: "Capability available", dot: "bg-accent" };
  return {
    label: connector.name === "Database" ? "Connected · probed" : "Connected",
    dot: DOT.connected,
  };
}
export function SystemHealth({ system }: { system: SystemStatus }) {
  return (
    <ul className="divide-y divide-border">
      {system.connectors.map((connector) => {
        const state = presentation(connector);
        const Icon = connector.name === "Database" ? Database : Plug;
        return (
          <li
            key={connector.name}
            className="flex flex-wrap items-start justify-between gap-3 py-3"
          >
            <div className="flex items-center gap-2">
              <Icon size={16} className="text-subtle" aria-hidden />
              <span className="text-sm">{connector.name}</span>
            </div>
            <div className="min-w-0 flex-1 text-right">
              <span className="inline-flex items-center gap-2 text-xs text-muted">
                <span
                  className={cn("h-1.5 w-1.5 shrink-0 rounded-full", state.dot)}
                />
                {state.label}
              </span>
              {connector.detail && (
                <p className="mt-1 break-anywhere text-[11px] text-subtle">
                  {connector.detail}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
