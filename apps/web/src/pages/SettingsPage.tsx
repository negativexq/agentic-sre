import type { ReactNode } from "react";

import { useSettings } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/States";

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border/60 py-2 last:border-0">
      <span className="text-sm text-muted">{label}</span>
      <span className="text-right text-sm text-text break-anywhere">{children}</span>
    </div>
  );
}

function YesNo({ value }: { value: boolean }) {
  return <Badge tone={value ? "healthy" : "neutral"}>{value ? "Yes" : "No"}</Badge>;
}

function Configured({ value }: { value: boolean }) {
  return (
    <Badge tone={value ? "healthy" : "neutral"}>{value ? "Configured" : "Not configured"}</Badge>
  );
}

export function SettingsPage() {
  const { data, isLoading, isError, error } = useSettings();

  return (
    <>
      <PageHeader
        title="Settings"
        description="Read-only effective configuration. Secrets are never shown — only whether they are set."
      />
      {isError && <ErrorState message={(error as Error).message} />}

      {isLoading && !data ? (
        <Skeleton className="h-64" />
      ) : data ? (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader title="Diagnosis" />
            <CardBody>
              <Row label="Auto-diagnosis">
                <YesNo value={data.auto_diagnose} />
              </Row>
              <Row label="Watch interval">
                {data.watch_interval_seconds > 0 ? `${data.watch_interval_seconds}s` : "off"}
              </Row>
              <Row label="Cluster access">{data.cluster_access}</Row>
              <Row label="Watched namespaces">
                {data.watched_namespaces.join(", ") || "—"}
              </Row>
              <Row label="Evidence namespaces">
                {data.evidence_namespaces.join(", ") || "—"}
              </Row>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Engine" />
            <CardBody>
              <Row label="LLM enabled">
                <YesNo value={data.llm_enabled} />
              </Row>
              <Row label="LLM model">{data.llm_model ?? "—"}</Row>
              <Row label="LLM max calls">{data.llm_max_calls}</Row>
              <Row label="Report version">v{data.report_version}</Row>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Security" />
            <CardBody>
              <Row label="API token">
                <Configured value={data.api_token_configured} />
              </Row>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Integrations" />
            <CardBody>
              <Row label="Email">
                <Configured value={data.email_configured} />
              </Row>
              {data.email_sender && <Row label="Email sender">{data.email_sender}</Row>}
              <Row label="Prometheus">
                <Configured value={data.prometheus_configured} />
              </Row>
              <Row label="Loki">
                <Configured value={data.loki_configured} />
              </Row>
              <Row label="Tempo">
                <Configured value={data.tempo_configured} />
              </Row>
            </CardBody>
          </Card>
        </div>
      ) : null}
    </>
  );
}
