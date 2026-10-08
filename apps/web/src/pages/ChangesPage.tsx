import { SearchInput } from "@/components/ui/Input";
import { useUrlFilters } from "@/lib/useUrlFilters";
import { Boxes, GitCompareArrows, SlidersHorizontal, X } from "lucide-react";
import { FilterSelect } from "@/components/ui/FilterSelect";
import { Button } from "@/components/ui/Button";

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

export function ChangesPage() {
  const { params, update } = useUrlFilters();
  const scope = params.get("scope") ?? "";
  const changeType = params.get("change_type") ?? "";
  const q = params.get("q") ?? "";

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
        <CardBody className="space-y-4">
          <div className="min-w-0 sm:max-w-xl">
            <SearchInput
              aria-label="Search resource"
              value={q}
              onChange={(event) => update({ q: event.target.value })}
              placeholder="Search changes by resource name…"
              className="h-10 bg-surface-raised"
            />
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
            <span className="flex items-center gap-2 text-xs text-subtle">
              <SlidersHorizontal size={14} aria-hidden /> Filters
            </span>
            <div className="grid min-w-0 flex-1 basis-full grid-cols-1 gap-2 sm:grid-cols-2 lg:max-w-2xl lg:basis-0">
              <FilterSelect
                label="Scope"
                icon={Boxes}
                value={scope}
                options={SCOPES}
                onChange={(value) => update({ scope: value })}
              />
              <FilterSelect
                label="Change type"
                icon={GitCompareArrows}
                value={changeType}
                options={TYPES}
                onChange={(value) => update({ change_type: value })}
              />
            </div>
            <Button
              variant="ghost"
              disabled={!scope && !changeType && !q}
              onClick={() => {
                update({ scope: null, change_type: null, q: null });
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
            <ChangesTable changes={data ?? []} />
          )}
        </CardBody>
      </Card>
    </>
  );
}
