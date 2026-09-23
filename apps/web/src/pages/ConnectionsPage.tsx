import { useSystemStatus } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { SystemHealth } from "@/components/SystemHealth";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/States";

export function ConnectionsPage() {
  const { data, isLoading, isError, error } = useSystemStatus();

  return (
    <>
      <PageHeader
        title="Connections"
        description="Health of the upstreams the control plane depends on. Probed where possible; otherwise reported as configured or not."
      />
      {isError && <ErrorState message={(error as Error).message} />}
      <Card>
        <CardHeader title="Connectors" />
        <CardBody>
          {isLoading && !data ? (
            <Skeleton className="h-40" />
          ) : data ? (
            <SystemHealth system={data} />
          ) : null}
        </CardBody>
      </Card>
    </>
  );
}
