import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { reportUrl } from "@/api/client";
import { useCreateReport, useIncidentReports } from "@/api/hooks";
import { Markdown } from "@/components/Markdown";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { Drawer } from "@/components/ui/Drawer";
import { Skeleton } from "@/components/ui/States";
import { ShareReport } from "@/components/workspace/ShareReport";
import { dateTime } from "@/lib/format";

function PreviewDrawer({ reportId, onClose }: { reportId: string | null; onClose: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ["report-markdown", reportId],
    queryFn: async () => {
      const response = await fetch(reportUrl(reportId as string, "markdown"));
      return response.text();
    },
    enabled: Boolean(reportId),
  });

  return (
    <Drawer open={Boolean(reportId)} onClose={onClose} title="Report preview">
      {isLoading || !data ? <Skeleton className="h-64" /> : <Markdown source={data} />}
    </Drawer>
  );
}

export function ReportExport({ incidentId }: { incidentId: string }) {
  const { data: reports } = useIncidentReports(incidentId);
  const create = useCreateReport(incidentId);
  const [previewId, setPreviewId] = useState<string | null>(null);

  const latest = reports?.[0];

  return (
    <Card>
      <CardHeader
        title="Report"
        action={
          <Button
            variant="primary"
            onClick={() => create.mutate()}
            disabled={create.isPending}
          >
            {create.isPending ? "Generating…" : "Generate report"}
          </Button>
        }
      />
      <CardBody className="space-y-3">
        <p className="text-sm text-muted">
          A report is an immutable snapshot pinned to this diagnosis run. Re-diagnosing the incident
          never changes an existing report.
        </p>

        {latest ? (
          <div className="rounded-lg border border-border bg-surface p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono text-xs text-subtle break-anywhere">
                {latest.report_id}
              </span>
              <span className="text-xs text-subtle">
                v{latest.report_version} · {dateTime(latest.generated_at)}
              </span>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => setPreviewId(latest.report_id)}>
                Preview
              </Button>
              <a href={reportUrl(latest.report_id, "pdf")} target="_blank" rel="noreferrer">
                <Button variant="secondary">PDF</Button>
              </a>
              <a href={reportUrl(latest.report_id, "markdown")} target="_blank" rel="noreferrer">
                <Button variant="secondary">Markdown</Button>
              </a>
              <a href={reportUrl(latest.report_id, "json")} target="_blank" rel="noreferrer">
                <Button variant="secondary">JSON</Button>
              </a>
            </div>
            {reports && reports.length > 1 && (
              <p className="mt-2 text-xs text-subtle">
                {reports.length} reports generated for this incident.
              </p>
            )}
            <ShareReport reportId={latest.report_id} />
          </div>
        ) : (
          <p className="text-sm text-subtle">No report yet. Generate one to preview and export.</p>
        )}
      </CardBody>

      <PreviewDrawer reportId={previewId} onClose={() => setPreviewId(null)} />
    </Card>
  );
}
