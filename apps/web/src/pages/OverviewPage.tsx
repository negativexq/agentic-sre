import { RecentDiagnoses } from "@/components/RecentDiagnoses";
import { useDashboard, useChanges } from "@/api/hooks";
import { useLiveUpdates } from "@/api/useLiveUpdates";
import { ChangesTable } from "@/components/ChangesTable";
import { Link } from "react-router-dom";
import { IncidentsTable } from "@/components/IncidentsTable";
import { LiveBadge } from "@/components/LiveBadge";
import { PageHeader } from "@/components/PageHeader";
import { StatTile } from "@/components/StatTile";
import { SystemHealth } from "@/components/SystemHealth";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/States";
import { humanizeSeconds } from "@/lib/format";

export function OverviewPage() {
  const recentChanges = useChanges({ limit: 5 });
  const { data, isLoading, isError, error } = useDashboard();
  const live = useLiveUpdates("/stream", [
    ["dashboard"],
    ["incidents"],
    ["changes"],
  ]);

  return (
    <>
      <PageHeader
        title="Overview"
        description="Active impact, diagnosis activity, and the evidence sources supporting investigations."
        action={<LiveBadge state={live} />}
      />

      {isError && <ErrorState message={(error as Error).message} />}

      {isLoading && !data ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-20" />
          ))}
        </div>
      ) : data ? (
        <div className="space-y-6">
          <div className="grid grid-cols-2 overflow-hidden rounded-lg border border-border bg-surface-raised sm:grid-cols-4">
            <StatTile
              label="Active incidents"
              value={data.counters.active_incidents}
            />
            <StatTile
              label="Critical"
              value={data.counters.critical_incidents}
              tone={
                data.counters.critical_incidents > 0 ? "critical" : "default"
              }
            />
            <StatTile
              label="Diagnosing"
              value={data.counters.diagnosing}
              hint="awaiting a stored diagnosis"
            />
            <StatTile
              label="Median diagnosis"
              value={humanizeSeconds(data.counters.median_diagnosis_seconds)}
              hint="incident opened → stored"
            />
          </div>

          <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_340px] 2xl:grid-cols-[minmax(0,1fr)_380px]">
            <div className="min-w-0 space-y-5">
              <Card>
                <CardHeader
                  title="Active incidents"
                  action={
                    <Link to="/incidents" className="text-xs text-accent">
                      Explore incidents →
                    </Link>
                  }
                />
                <CardBody className="p-0">
                  <IncidentsTable items={data.active_incidents} />
                </CardBody>
              </Card>
              <Card>
                <CardHeader
                  title="Recent changes"
                  action={
                    <Link to="/changes" className="text-xs text-accent">
                      Explore changes →
                    </Link>
                  }
                />
                <CardBody className="p-0">
                  {recentChanges.isLoading ? (
                    <Skeleton className="h-24" />
                  ) : recentChanges.isError ? (
                    <ErrorState
                      message={(recentChanges.error as Error).message}
                    />
                  ) : (
                    <ChangesTable changes={recentChanges.data ?? []} />
                  )}
                </CardBody>
              </Card>
            </div>
            <aside
              className="min-w-0 space-y-5"
              aria-label="Diagnosis activity and source health"
            >
              <Card>
                <CardHeader title="Recent diagnoses" />
                <CardBody>
                  <RecentDiagnoses items={data.recent_diagnoses} />
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="Source health" />
                <CardBody>
                  <SystemHealth system={data.system} />
                </CardBody>
              </Card>
            </aside>
          </div>
        </div>
      ) : null}
    </>
  );
}
