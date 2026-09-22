import { Badge } from "@/components/ui/Badge";
import type { FindingView } from "@/api/types";
import { clock, shortEntity } from "@/lib/format";

function roleTone(role: string) {
  switch (role) {
    case "INITIATING":
      return "accent" as const;
    case "SUPPORTING":
      return "healthy" as const;
    case "CONSEQUENCE":
      return "neutral" as const;
    default:
      return "neutral" as const;
  }
}

function offsetLabel(seconds: number | null): string | null {
  if (seconds === null) return null;
  const sign = seconds < 0 ? "−" : "+";
  return `${sign}${Math.abs(seconds).toFixed(0)}s vs onset`;
}

export function FindingsList({ findings }: { findings: FindingView[] }) {
  if (findings.length === 0) {
    return <p className="text-sm text-muted">None recorded.</p>;
  }
  return (
    <ul className="space-y-2">
      {findings.map((finding, index) => (
        <li key={index} className="rounded-lg border border-border bg-surface px-3 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="neutral">{finding.kind}</Badge>
            <Badge tone={roleTone(finding.temporal_role)}>{finding.temporal_role}</Badge>
            {offsetLabel(finding.onset_delta_seconds) && (
              <span className="font-mono text-xs text-subtle">
                {offsetLabel(finding.onset_delta_seconds)}
              </span>
            )}
            {finding.at && <span className="font-mono text-xs text-subtle">{clock(finding.at)}</span>}
          </div>
          <p className="mt-1 text-sm text-text break-anywhere">{finding.summary}</p>
          <p className="mt-0.5 font-mono text-xs text-subtle break-anywhere">
            {shortEntity(finding.entity)}
            {finding.evidence_ids.length > 0 && ` · ${finding.evidence_ids.slice(0, 3).join(", ")}`}
          </p>
        </li>
      ))}
    </ul>
  );
}
