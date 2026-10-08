import { SearchInput } from "@/components/ui/Input";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { useUrlFilters } from "@/lib/useUrlFilters";
import { ReportExportMenu } from "@/components/ReportExportMenu";
import { CopyButton } from "@/components/ui/CopyButton";
import { useReports } from "@/api/hooks";
import { useLiveUpdates } from "@/api/useLiveUpdates";
import { PreviewDrawer } from "@/components/workspace/ReportExport";
import { ShareReport } from "@/components/workspace/ShareReport";
import { Drawer } from "@/components/ui/Drawer";
import { LiveBadge } from "@/components/LiveBadge";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardBody } from "@/components/ui/Card";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/States";
import { TCell, THead, TRow, Table } from "@/components/ui/Table";
import { dateTime, shortEntity } from "@/lib/format";
import { confidenceTone, resolutionTone } from "@/lib/tones";

export function ReportsPage() {
  const { data, isLoading, isError, error } = useReports();
  const live = useLiveUpdates("/stream", [["reports"]]);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [shareId, setShareId] = useState<string | null>(null);
  const { params, update } = useUrlFilters();
  const q = params.get("q") ?? "";

  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase();
    if (!term) return data ?? [];
    return (data ?? []).filter(
      (report) =>
        report.title.toLowerCase().includes(term) ||
        (report.root_actor ?? report.leading_root_actor ?? "")
          .toLowerCase()
          .includes(term) ||
        report.report_id.toLowerCase().includes(term) ||
        (report.diagnosis_run_id ?? "").toLowerCase().includes(term) ||
        report.incident_id.toLowerCase().includes(term),
    );
  }, [data, q]);

  return (
    <>
      <PageHeader
        title="Reports"
        description="Immutable report snapshots. Each is pinned to the diagnosis run it was taken from."
      />

      <Card className="mb-4">
        <CardBody className="flex items-center justify-between gap-3">
          <div className="min-w-0 flex-1 sm:max-w-xl">
            <SearchInput
              aria-label="Search reports"
              value={q}
              onChange={(event) => update({ q: event.target.value })}
              placeholder="Search title, actor, report ID or run ID"
              className="h-10 bg-surface-raised"
            />
          </div>
          <LiveBadge state={live} />
        </CardBody>
      </Card>

      {isError && <ErrorState message={(error as Error).message} />}

      <Card>
        <CardBody className="p-0">
          {isLoading && !data ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 4 }).map((_, index) => (
                <Skeleton key={index} className="h-12" />
              ))}
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              title={q.trim() ? "No matching reports." : "No reports yet."}
              description={
                q.trim()
                  ? "Try another title, actor or identity, or clear your search."
                  : "Open an incident and generate a report to see it here."
              }
            />
          ) : (
            <Table>
              <THead
                columns={[
                  "Incident",
                  "Root actor",
                  "Confidence",
                  "Resolution",
                  "Generated",
                  "Export",
                ]}
              />
              <tbody>
                {filtered.map((report) => (
                  <TRow key={report.report_id}>
                    <TCell className="max-w-[16rem]">
                      <Link
                        to={`/incidents/${report.incident_id}`}
                        className="font-medium text-accent hover:underline break-anywhere"
                      >
                        {report.title}
                      </Link>
                      <p className="mt-1 break-anywhere font-mono text-[10px] text-subtle">
                        Report {report.report_id}{" "}
                        <CopyButton
                          value={report.report_id}
                          label="report ID"
                        />
                        <br />
                        Run {report.diagnosis_run_id ?? "not recorded"}{" "}
                        {report.diagnosis_run_id && (
                          <CopyButton
                            value={report.diagnosis_run_id}
                            label="diagnosis run"
                          />
                        )}{" "}
                        · v{report.report_version}
                      </p>
                    </TCell>
                    <TCell className="max-w-[14rem]">
                      <span className="font-mono text-xs break-anywhere">
                        {shortEntity(
                          report.root_actor ?? report.leading_root_actor,
                        )}
                      </span>
                    </TCell>
                    <TCell>
                      <Badge tone={confidenceTone(report.confidence)}>
                        {report.confidence}
                      </Badge>
                    </TCell>
                    <TCell>
                      <Badge tone={resolutionTone(report.resolution)}>
                        {report.resolution}
                      </Badge>
                    </TCell>
                    <TCell className="whitespace-nowrap text-muted">
                      {dateTime(report.generated_at)}
                    </TCell>
                    <TCell>
                      <div className="flex flex-wrap gap-1">
                        <Button
                          variant="ghost"
                          className="h-7 px-2 py-0 text-xs"
                          onClick={() => setPreviewId(report.report_id)}
                        >
                          Preview
                        </Button>
                        <Button
                          variant="ghost"
                          className="h-7 px-2 py-0 text-xs"
                          onClick={() => setShareId(report.report_id)}
                        >
                          Share
                        </Button>
                        <ReportExportMenu reportId={report.report_id} />
                      </div>
                    </TCell>
                  </TRow>
                ))}
              </tbody>
            </Table>
          )}
        </CardBody>
      </Card>
      <PreviewDrawer reportId={previewId} onClose={() => setPreviewId(null)} />
      <Drawer
        open={Boolean(shareId)}
        onClose={() => setShareId(null)}
        title="Share immutable report"
      >
        {shareId && <ShareReport reportId={shareId} />}
      </Drawer>
    </>
  );
}
