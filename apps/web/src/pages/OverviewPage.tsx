import { useDashboard } from "@/api/hooks";
import { IncidentsTable } from "@/components/IncidentsTable";
import { PageHeader } from "@/components/PageHeader";
import { StatTile } from "@/components/StatTile";
import { SystemHealth } from "@/components/SystemHealth";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/States";
import { humanizeSeconds } from "@/lib/format";

export function OverviewPage() {
  const { data, isLoading, isError, error } = useDashboard();

  return (
    <>
      <PageHeader
        title="Overview"
        description="What is happening across the cluster right now."
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
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile label="Active incidents" value={data.counters.active_incidents} />
            <StatTile
              label="Critical"
              value={data.counters.critical_incidents}
              tone={data.counters.critical_incidents > 0 ? "critical" : "default"}
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

          <Card>
            <CardHeader
              title="Active incidents"
              action={
                <span className="text-xs text-subtle">{data.active_incidents.length} open</span>
              }
            />
            <CardBody className="p-0">
              <IncidentsTable items={data.active_incidents} />
            </CardBody>
          </Card>

          <div className="grid gap-6 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="Recent diagnoses" />
              <CardBody className="p-0">
                <IncidentsTable items={data.recent_diagnoses} />
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="System health" />
              <CardBody>
                <SystemHealth system={data.system} />
              </CardBody>
            </Card>
          </div>
        </div>
      ) : null}
    </>
  );
}
