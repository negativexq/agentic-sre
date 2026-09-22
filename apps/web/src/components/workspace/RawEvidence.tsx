import { useIncidentEvidence } from "@/api/hooks";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/States";
import { dateTime } from "@/lib/format";

/** Raw provenance rows, fetched lazily only when the operator drills in. */
export function RawEvidence({ incidentId, active }: { incidentId: string; active: boolean }) {
  const { data, isLoading, isError, error } = useIncidentEvidence(incidentId, active);

  if (isLoading) return <Skeleton className="h-24" />;
  if (isError) return <ErrorState message={(error as Error).message} />;
  if (!data || data.length === 0) {
    return (
      <EmptyState
        title="No raw evidence rows."
        description="The deterministic engine reasons over findings; raw provenance rows appear here when captured."
      />
    );
  }

  return (
    <ul className="space-y-3">
      {data.map((evidence) => (
        <li key={evidence.evidence_id} className="rounded-lg border border-border bg-surface p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="font-medium text-text">
              {evidence.source_type} · {evidence.source_system}
            </span>
            <span className="font-mono text-xs text-subtle">{dateTime(evidence.collected_at)}</span>
          </div>
          <p className="mt-1 font-mono text-xs text-subtle break-anywhere">
            ref {evidence.raw_result_reference}
          </p>
          <div className="mt-2">
            <CodeBlock value={JSON.stringify(evidence.observation, null, 2)} />
          </div>
        </li>
      ))}
    </ul>
  );
}
