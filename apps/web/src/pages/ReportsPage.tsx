import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { reportUrl } from "@/api/client";
import { useReports } from "@/api/hooks";
import { useLiveUpdates } from "@/api/useLiveUpdates";
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
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const term = q.trim().toLowerCase();
    if (!term) return data ?? [];
    return (data ?? []).filter(
      (report) =>
        report.title.toLowerCase().includes(term) ||
        (report.root_actor ?? report.leading_root_actor ?? "").toLowerCase().includes(term),
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
          <input
            value={q}
            onChange={(event) => setQ(event.target.value)}
            placeholder="Search by incident or actor"
            className="w-full max-w-sm rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-text focus-visible:outline-2 focus-visible:outline-accent"
          />
          <span className="shrink-0 text-xs text-subtle">
            {live === "live" ? "Live" : "Polling"}
          </span>
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
              title="No reports yet."
              description="Open an incident and generate a report to see it here."
            />
          ) : (
            <Table>
              <THead
                columns={["Incident", "Root actor", "Confidence", "Resolution", "Generated", "Export"]}
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
                      <p className="font-mono text-xs text-subtle">v{report.report_version}</p>
                    </TCell>
                    <TCell className="max-w-[14rem]">
                      <span className="font-mono text-xs break-anywhere">
                        {shortEntity(report.root_actor ?? report.leading_root_actor)}
                      </span>
                    </TCell>
                    <TCell>
                      <Badge tone={confidenceTone(report.confidence)}>{report.confidence}</Badge>
                    </TCell>
                    <TCell>
                      <Badge tone={resolutionTone(report.resolution)}>{report.resolution}</Badge>
                    </TCell>
                    <TCell className="whitespace-nowrap text-muted">
                      {dateTime(report.generated_at)}
                    </TCell>
                    <TCell>
                      <div className="flex gap-1">
                        {(["pdf", "markdown", "json"] as const).map((format) => (
                          <a
                            key={format}
                            href={reportUrl(report.report_id, format)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            <Button variant="ghost" className="h-7 px-2 py-0 text-xs uppercase">
                              {format === "markdown" ? "MD" : format}
                            </Button>
                          </a>
                        ))}
                      </div>
                    </TCell>
                  </TRow>
                ))}
              </tbody>
            </Table>
          )}
        </CardBody>
      </Card>
    </>
  );
}
