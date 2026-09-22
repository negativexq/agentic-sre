import { useState } from "react";

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

function Select({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-subtle">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-text focus-visible:outline-2 focus-visible:outline-accent"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option || "Any"}
          </option>
        ))}
      </select>
    </label>
  );
}

export function IncidentsPage() {
  const [severity, setSeverity] = useState("");
  const [resolution, setResolution] = useState("");
  const [activeOnly, setActiveOnly] = useState(false);
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);

  const filters: IncidentFilters = {
    severity: severity || undefined,
    resolution: resolution || undefined,
    active: activeOnly || undefined,
    q: q || undefined,
    limit: PAGE_SIZE,
    offset,
  };
  const { data, isLoading, isError, error } = useIncidents(filters);
  const live = useLiveUpdates("/stream", [["incidents"]]);

  const reset = (mutate: () => void) => {
    setOffset(0);
    mutate();
  };

  const total = data?.total ?? 0;
  const shown = data?.items.length ?? 0;

  return (
    <>
      <PageHeader
        title="Incidents"
        description="Every incident, filterable by impact and diagnosis outcome."
        action={<LiveBadge state={live} />}
      />

      <Card className="mb-4">
        <CardBody className="flex flex-wrap items-end gap-3">
          <Select
            label="Severity"
            value={severity}
            options={SEVERITIES}
            onChange={(v) => reset(() => setSeverity(v))}
          />
          <Select
            label="Resolution"
            value={resolution}
            options={RESOLUTIONS}
            onChange={(v) => reset(() => setResolution(v))}
          />
          <label className="flex flex-col gap-1 text-xs text-subtle">
            Search title
            <input
              value={q}
              onChange={(event) => reset(() => setQ(event.target.value))}
              placeholder="e.g. payment"
              className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-text focus-visible:outline-2 focus-visible:outline-accent"
            />
          </label>
          <label className="flex items-center gap-2 pb-1.5 text-sm text-muted">
            <input
              type="checkbox"
              checked={activeOnly}
              onChange={(event) => reset(() => setActiveOnly(event.target.checked))}
            />
            Active only
          </label>
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
          {total === 0 ? "No incidents" : `Showing ${offset + 1}–${offset + shown} of ${total}`}
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
