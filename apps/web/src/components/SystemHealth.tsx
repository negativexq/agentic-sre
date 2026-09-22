import { cn } from "@/lib/cn";
import type { ConnectorStatus, SystemStatus } from "@/api/types";

const DOT: Record<ConnectorStatus, string> = {
  connected: "bg-healthy",
  degraded: "bg-warning",
  unavailable: "bg-critical",
  not_configured: "bg-border-strong",
};

const LABEL: Record<ConnectorStatus, string> = {
  connected: "Connected",
  degraded: "Degraded",
  unavailable: "Unavailable",
  not_configured: "Not configured",
};

export function SystemHealth({ system }: { system: SystemStatus }) {
  return (
    <ul className="divide-y divide-border">
      {system.connectors.map((connector) => (
        <li key={connector.name} className="flex items-center justify-between gap-3 py-2">
          <div className="flex items-center gap-2">
            <span className={cn("h-2 w-2 rounded-full", DOT[connector.status])} />
            <span className="text-sm text-text">{connector.name}</span>
          </div>
          <div className="text-right">
            <span className="text-sm text-muted">{LABEL[connector.status]}</span>
            {connector.detail && <p className="text-xs text-subtle">{connector.detail}</p>}
          </div>
        </li>
      ))}
    </ul>
  );
}
