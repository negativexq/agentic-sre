import { useConnectors, useSystemStatus } from "@/api/hooks";
import { ConnectButton, ConnectorsTable } from "@/components/ConnectorsPanel";
import { PageHeader } from "@/components/PageHeader";
import { SystemHealth } from "@/components/SystemHealth";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import {
  Alert,
  EmptyState,
  ErrorState,
  Skeleton,
} from "@/components/ui/States";

export function ConnectionsPage() {
  const system = useSystemStatus();
  const connectors = useConnectors();
  const view = connectors.data;
  const demo = system.data?.mode === "demo";

  return (
    <>
      <PageHeader
        title="Connections"
        description="The clusters connected to this control plane, and the data sources it reads through them."
      />
      <div className="space-y-6">
        {demo && (
          <Alert tone="info">
            This console runs on demo data: no cluster is connected, by design.
          </Alert>
        )}
        <Card>
          <CardHeader
            title="Connectors"
            action={
              view?.available && view.writable ? <ConnectButton /> : undefined
            }
          />
          <CardBody className="space-y-4">
            {connectors.isError && (
              <ErrorState message={(connectors.error as Error).message} />
            )}
            {connectors.isLoading && !view ? (
              <Skeleton className="h-24" />
            ) : view && !view.available ? (
              <EmptyState
                title="No Connector registry"
                description={
                  demo
                    ? "The demo has no clusters to connect."
                    : "This control plane holds no CA key (SRE_CONNECTOR_CA_KEY), so it cannot enroll Connectors."
                }
              />
            ) : view ? (
              <>
                {!view.writable && (
                  <Alert tone="info">
                    Creating and disabling Connectors here needs{" "}
                    <code>SRE_API_TOKEN</code> on the control plane; until then
                    use <code>agentic-sre connector</code>.
                  </Alert>
                )}
                <ConnectorsTable view={view} />
              </>
            ) : null}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Data sources" />
          <CardBody>
            {system.isError && (
              <ErrorState message={(system.error as Error).message} />
            )}
            {system.isLoading && !system.data ? (
              <Skeleton className="h-40" />
            ) : system.data ? (
              <SystemHealth system={system.data} />
            ) : null}
          </CardBody>
        </Card>
      </div>
    </>
  );
}
