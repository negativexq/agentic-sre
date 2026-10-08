import { SearchInput, Input } from "@/components/ui/Input";
import { useUrlFilters } from "@/lib/useUrlFilters";
import {
  CircleDot,
  ShieldCheck,
  TriangleAlert,
  GitBranch,
  SlidersHorizontal,
  X,
  Activity,
} from "lucide-react";
import { FilterSelect } from "@/components/ui/FilterSelect";

import { useIncidents } from "@/api/hooks";
import { useLiveUpdates } from "@/api/useLiveUpdates";
import type { IncidentFilters } from "@/api/types";
import { IncidentsTable } from "@/components/IncidentsTable";
import { LiveBadge } from "@/components/LiveBadge";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/Button";
import { Card, CardBody } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/States";

const SEVERITIES = ["", "CRITICAL", "WARNING", "INFO"];
const RESOLUTIONS = ["", "RESOLVED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE"];
const PAGE_SIZE = 25;

export function IncidentsPage() {
  const { params, update } = useUrlFilters();
  const status = params.get("status") ?? "";
  const confidence = params.get("confidence") ?? "";
  const service = params.get("service") ?? "";
  const severity = params.get("severity") ?? "";
  const resolution = params.get("resolution") ?? "";
  const activeOnly = params.get("active") === "true";
  const q = params.get("q") ?? "";
  const rawOffset = Number(params.get("offset") ?? 0);
  const offset =
    Number.isSafeInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0;
  const setOffset = (value: number) =>
    update({ offset: value ? String(value) : null });
  const setFilter = (key: string, value: string) =>
    update({ [key]: value, offset: null });

  const filters: IncidentFilters = {
    status: status || undefined,
    confidence: confidence || undefined,
    service: service || undefined,
    severity: severity || undefined,
    resolution: resolution || undefined,
    active: activeOnly || undefined,
    q: q || undefined,
    limit: PAGE_SIZE,
    offset,
  };
  const { data, isLoading, isError, error } = useIncidents(filters);
  const live = useLiveUpdates("/stream", [["incidents"]]);

  const total = data?.total ?? 0;
  const shown = data?.items.length ?? 0;
  const filterCount = [
    status,
    confidence,
    severity,
    resolution,
    service,
    q,
    activeOnly,
  ].filter(Boolean).length;

  return (
    <>
      <PageHeader
        title="Incidents"
        description="Every incident, filterable by impact and diagnosis outcome."
        action={<LiveBadge state={live} />}
      />

      <Card className="mb-4">
        <CardBody className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <div className="min-w-0 flex-1 basis-64">
              <SearchInput
                aria-label="Search title"
                value={q}
                onChange={(event) => setFilter("q", event.target.value)}
                placeholder="Search incidents by title…"
                className="h-10 bg-surface-raised"
              />
            </div>
            <div className="min-w-0 flex-1 basis-48 sm:max-w-72">
              <Input
                aria-label="Service"
                value={service}
                onChange={(event) => setFilter("service", event.target.value)}
                placeholder="Exact service name"
                className="h-10 bg-surface-raised"
              />
            </div>
            <button
              type="button"
              aria-pressed={activeOnly}
              onClick={() => setFilter("active", activeOnly ? "" : "true")}
              className={`flex h-10 items-center gap-2 rounded-md border px-3 text-sm transition-colors ${activeOnly ? "border-accent/40 bg-accent-soft text-accent" : "border-border-strong text-muted hover:bg-surface"}`}
            >
              <Activity size={15} aria-hidden />
              Active only
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
            <span className="flex items-center gap-2 text-xs text-subtle">
              <SlidersHorizontal size={14} aria-hidden /> Filters
              {filterCount > 0 && (
                <span className="rounded bg-accent-soft px-1.5 text-accent">
                  {filterCount}
                </span>
              )}
            </span>
            <div className="grid min-w-0 flex-1 basis-full grid-cols-1 gap-2 sm:grid-cols-2 2xl:basis-0 2xl:grid-cols-4">
              <FilterSelect
                label="Status"
                icon={CircleDot}
                value={status}
                options={[
                  "",
                  "OPEN",
                  "TRIAGING",
                  "INVESTIGATING",
                  "HYPOTHESIS_FORMED",
                  "VALIDATING",
                  "REMEDIATION_PROPOSED",
                  "POLICY_EVALUATION",
                  "APPROVAL_REQUIRED",
                  "EXECUTING",
                  "VERIFYING",
                  "RESOLVED",
                  "ESCALATED",
                  "FAILED",
                  "CLOSED",
                ]}
                onChange={(v) => setFilter("status", v)}
              />
              <FilterSelect
                label="Confidence"
                icon={ShieldCheck}
                value={confidence}
                options={["", "VERIFIED", "LIKELY", "UNVERIFIED"]}
                onChange={(v) => setFilter("confidence", v)}
              />
              <FilterSelect
                label="Severity"
                icon={TriangleAlert}
                value={severity}
                options={SEVERITIES}
                onChange={(v) => setFilter("severity", v)}
              />
              <FilterSelect
                label="Resolution"
                icon={GitBranch}
                value={resolution}
                options={RESOLUTIONS}
                onChange={(v) => setFilter("resolution", v)}
              />
            </div>
            <Button
              variant="ghost"
              disabled={filterCount === 0}
              onClick={() => {
                update({
                  status: null,
                  confidence: null,
                  severity: null,
                  resolution: null,
                  service: null,
                  q: null,
                  active: null,
                  offset: null,
                });
              }}
            >
              <X size={14} aria-hidden /> Clear filters
            </Button>
          </div>
        </CardBody>
      </Card>

      {isError && <ErrorState message={(error as Error).message} />}

      <Card>
        <CardBody className="p-0">
          {isLoading && !data ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 5 }).map((_, index) => (
                <Skeleton key={index} className="h-10" />
              ))}
            </div>
          ) : (
            <IncidentsTable items={data?.items ?? []} />
          )}
        </CardBody>
      </Card>

      <div className="mt-3 flex items-center justify-between text-sm text-muted">
        <span>
          {total === 0
            ? "No incidents"
            : `Showing ${offset + 1}–${offset + shown} of ${total}`}
        </span>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(offset - PAGE_SIZE, 0))}
          >
            Previous
          </Button>
          <Button
            variant="secondary"
            disabled={offset + shown >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next
          </Button>
        </div>
      </div>
    </>
  );
}
