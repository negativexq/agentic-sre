import { useState } from "react";

import { useChanges } from "@/api/hooks";
import { useLiveUpdates } from "@/api/useLiveUpdates";
import type { ChangeFilters } from "@/api/types";
import { ChangesTable } from "@/components/ChangesTable";
import { LiveBadge } from "@/components/LiveBadge";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardBody } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/States";

const SCOPES = ["", "DEPLOYMENT", "CONFIGURATION"];
const TYPES = ["", "CREATED", "UPDATED", "ROLLOUT", "SCALED"];

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

export function ChangesPage() {
  const [scope, setScope] = useState("");
  const [changeType, setChangeType] = useState("");
  const [q, setQ] = useState("");

  const filters: ChangeFilters = {
    scope: scope || undefined,
    change_type: changeType || undefined,
    q: q || undefined,
    limit: 200,
  };
  const { data, isLoading, isError, error } = useChanges(filters);
  const live = useLiveUpdates("/stream", [["changes"]]);

  return (
    <>
      <PageHeader
        title="Changes"
        description="Every recorded change fact across the cluster. Read-only; no cause is inferred here."
        action={<LiveBadge state={live} />}
      />

      <Card className="mb-4">
        <CardBody className="flex flex-wrap items-end gap-3">
          <Select label="Scope" value={scope} options={SCOPES} onChange={setScope} />
          <Select label="Change type" value={changeType} options={TYPES} onChange={setChangeType} />
          <label className="flex flex-col gap-1 text-xs text-subtle">
            Search resource
            <input
              value={q}
              onChange={(event) => setQ(event.target.value)}
              placeholder="e.g. payment"
              className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-text focus-visible:outline-2 focus-visible:outline-accent"
            />
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
            <ChangesTable changes={data ?? []} />
          )}
        </CardBody>
      </Card>
    </>
  );
}
