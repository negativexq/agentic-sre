import { ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import type { FindingView } from "@/api/types";
import { dateTime } from "@/lib/format";

export function FindingsList({
  findings,
  onSelect,
}: {
  findings: FindingView[];
  onSelect?: (finding: FindingView) => void;
}) {
  if (!findings.length)
    return <p className="py-2 text-sm text-muted">None recorded.</p>;
  return (
    <ul className="divide-y divide-border">
      {findings.map((finding, index) => {
        const content = (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="neutral">{finding.kind}</Badge>
              <span className="text-[10px] uppercase tracking-wide text-subtle">
                {finding.temporal_role}
              </span>
              {finding.onset_delta_seconds !== null && (
                <span className="font-mono text-[11px] text-muted">
                  {finding.onset_delta_seconds > 0 ? "+" : ""}
                  {finding.onset_delta_seconds}s vs onset
                </span>
              )}
              {onSelect && (
                <ChevronRight
                  size={14}
                  className="ml-auto shrink-0 text-subtle"
                  aria-hidden
                />
              )}
            </div>
            <p className="mt-2 break-anywhere text-sm">{finding.summary}</p>
            <p className="mt-1 break-anywhere font-mono text-[11px] text-muted">
              {finding.entity}
            </p>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-subtle">
              <span>
                {finding.at ? dateTime(finding.at) : "Observation time unknown"}
              </span>
              <span>
                {finding.evidence_ids.length} evidence reference
                {finding.evidence_ids.length === 1 ? "" : "s"}
              </span>
            </div>
          </>
        );
        return (
          <li key={index}>
            {onSelect ? (
              <button
                type="button"
                aria-label={`Inspect finding: ${finding.summary}`}
                onClick={() => onSelect(finding)}
                className="block w-full rounded-md px-2 py-3 text-left transition-colors hover:bg-surface"
              >
                {content}
              </button>
            ) : (
              <div className="px-2 py-3">{content}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
