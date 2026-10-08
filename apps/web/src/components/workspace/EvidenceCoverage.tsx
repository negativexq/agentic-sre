import type { EvidenceCoverage as Coverage } from "@/api/types";
import { useEvidenceCoverage } from "@/api/hooks";
import { Badge } from "@/components/ui/Badge";
import { ErrorState, Skeleton } from "@/components/ui/States";
import { dateTime } from "@/lib/format";

export function CoverageRecord({ coverage }: { coverage: Coverage | null }) {
  if (!coverage)
    return (
      <p className="text-sm text-muted">
        No evidence coverage record was stored for this diagnosis. Observation
        continuity and delivery completeness cannot be established from an
        absent record.
      </p>
    );
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted">
        Observation window {dateTime(coverage.starts_at)} –{" "}
        {dateTime(coverage.window_end)}
        <br />
        Stream followed since{" "}
        {coverage.stream_followed_since
          ? dateTime(coverage.stream_followed_since)
          : "not recorded"}
      </p>
      <p className="text-xs text-subtle">
        Source continuity and transport completeness are independent recorded
        dimensions. Coverage does not establish causation.
      </p>
      {coverage.scopes.length ? (
        coverage.scopes.map((scope, index) => (
          <section key={index} className="rounded-md border border-border p-4">
            <h3 className="break-anywhere font-mono text-sm">
              {scope.namespace || "Cluster scope"}/{scope.kind}
            </h3>
            <dl className="mt-3 grid gap-3 sm:grid-cols-2">
              <div>
                <dt className="text-xs text-subtle">Source continuity</dt>
                <dd className="mt-1">
                  <Badge
                    tone={
                      scope.source_continuity === "CONTINUOUS"
                        ? "healthy"
                        : scope.source_continuity === "GAPPED"
                          ? "warning"
                          : "neutral"
                    }
                  >
                    {scope.source_continuity}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-subtle">Transport completeness</dt>
                <dd className="mt-1">
                  <Badge
                    tone={
                      scope.transport_completeness === "PROVEN"
                        ? "healthy"
                        : "neutral"
                    }
                  >
                    {scope.transport_completeness}
                  </Badge>
                </dd>
              </div>
            </dl>
            <p className="mt-3 text-xs text-muted">
              Transport proof time:{" "}
              {scope.transport_proven_at
                ? dateTime(scope.transport_proven_at)
                : "not recorded"}
            </p>
            {scope.gaps.length > 0 && (
              <details className="mt-3">
                <summary className="text-xs text-accent">
                  Recorded gaps ({scope.gaps.length})
                </summary>
                <ul className="mt-2 divide-y divide-border">
                  {scope.gaps.map((gap, i) => (
                    <li key={i} className="py-2 text-xs text-muted">
                      <p className="break-anywhere">{gap.reason}</p>
                      <p>
                        Since{" "}
                        {gap.since ? dateTime(gap.since) : "unknown start"} ·
                        recorded {dateTime(gap.at)}
                      </p>
                      <p className="break-anywhere">
                        {gap.namespace === null
                          ? "All scopes"
                          : `${gap.namespace}/${gap.kind ?? "Kind not recorded"}`}
                      </p>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </section>
        ))
      ) : (
        <p className="text-sm text-muted">
          No scope coverage records included in this window.
        </p>
      )}
    </div>
  );
}
export function EvidenceCoverage({ incidentId }: { incidentId: string }) {
  const query = useEvidenceCoverage(incidentId);
  if (query.isPending) return <Skeleton className="h-32" />;
  if (query.isError)
    return (
      <ErrorState
        message={`Could not load coverage: ${(query.error as Error).message}`}
      />
    );
  return <CoverageRecord coverage={query.data ?? null} />;
}
