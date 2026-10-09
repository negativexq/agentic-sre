import { useState } from "react";
import { Drawer } from "@/components/ui/Drawer";
import { Entity } from "@/components/Entity";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/States";
import { TCell, THead, TRow, Table } from "@/components/ui/Table";
import type { ChangeView } from "@/api/types";
import { dateTime } from "@/lib/format";
import { CopyButton } from "@/components/ui/CopyButton";
import { useInvestigationCanvas } from "@/components/investigation/context";

function onsetLabel(seconds: number | null): string {
  if (seconds === null) return "";
  const abs = Math.abs(seconds);
  const unit =
    abs < 90
      ? `${abs.toFixed(0)}s`
      : abs < 5400
        ? `${(abs / 60).toFixed(0)}m`
        : `${(abs / 3600).toFixed(1)}h`;
  return `${seconds < 0 ? "−" : "+"}${unit}`;
}

/**
 * A change timeline. When `showOnset` is set, each row is placed relative to the
 * incident onset and rows touching the engine's leading actor are marked — a
 * label, never a causal claim the UI makes on its own.
 */
export function ChangesTable({
  changes,
  showOnset = false,
}: {
  changes: ChangeView[];
  showOnset?: boolean;
}) {
  const [selected, setSelected] = useState<ChangeView | null>(null);
  const canvas = useInvestigationCanvas();
  if (changes.length === 0) {
    return (
      <EmptyState
        title="No changes recorded."
        description="Change facts are captured by the harness and read-only here."
      />
    );
  }

  const columns = showOnset
    ? ["vs onset", "Resource", "Change", "Scope", "When"]
    : ["When", "Resource", "Change", "Scope", "Source"];

  return (
    <>
      <Table>
        <THead columns={columns} />
        <tbody>
          {changes.map((change) => (
            <TRow key={change.change_id}>
              {showOnset ? (
                <TCell className="whitespace-nowrap font-mono text-xs text-subtle">
                  {onsetLabel(change.onset_delta_seconds) || "Unknown"}
                </TCell>
              ) : (
                <TCell className="whitespace-nowrap text-muted">
                  {dateTime(change.timestamp)}
                </TCell>
              )}
              <TCell className="max-w-[18rem]">
                <button
                  className="block w-full text-left hover:text-accent"
                  onClick={() => {
                    setSelected(change);
                    canvas?.select({ kind: "change", id: change.change_id });
                  }}
                >
                  <Entity
                    value={`${change.namespace ? `${change.namespace}/` : ""}${change.resource_type}/${change.resource_name}`}
                  />
                  <span className="mt-1 block text-[10px] text-accent">
                    Inspect change
                  </span>
                </button>
                {change.matches_leading_actor && (
                  <Badge tone="accent" className="ml-2">
                    actor kind/name match
                  </Badge>
                )}
              </TCell>
              <TCell>
                <Badge tone="neutral">{change.change_type}</Badge>
              </TCell>
              <TCell className="text-muted">{change.scope}</TCell>
              <TCell className="text-muted">
                {showOnset
                  ? dateTime(change.timestamp)
                  : (change.source ?? "—")}
              </TCell>
            </TRow>
          ))}
        </tbody>
      </Table>
      <Drawer
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title="Change details"
      >
        {selected && (
          <div className="space-y-5">
            <Entity
              value={`${selected.namespace ? `${selected.namespace}/` : ""}${selected.resource_type}/${selected.resource_name}`}
            />
            <CopyButton
              value={`${selected.namespace ? `${selected.namespace}/` : ""}${selected.resource_type}/${selected.resource_name}`}
              label="resource identity"
            />
            <Badge>{selected.change_type}</Badge>
            <p className="text-sm text-muted">
              A recorded change fact. Temporal proximity to an incident does not
              establish causation.
            </p>
            <dl className="space-y-4 text-sm">
              <div>
                <dt className="text-xs text-subtle">Occurrence time</dt>
                <dd className="break-anywhere">{selected.timestamp}</dd>
              </div>
              <div>
                <dt className="text-xs text-subtle">Scope</dt>
                <dd className="break-anywhere">{selected.scope}</dd>
              </div>
              <div>
                <dt className="text-xs text-subtle">Recorded namespace</dt>
                <dd className="break-anywhere">
                  {selected.namespace ??
                    "Not recorded — kind/name alone does not establish resource identity."}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-subtle">Source</dt>
                <dd>{selected.source ?? "Not recorded"}</dd>
              </div>
              <div>
                <dt className="text-xs text-subtle">Revision</dt>
                <dd className="break-anywhere font-mono text-xs">
                  {selected.revision ?? "Not recorded"}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-subtle">Change ID</dt>
                <dd className="break-anywhere font-mono text-xs">
                  {selected.change_id}
                  <CopyButton value={selected.change_id} label="change ID" />
                </dd>
              </div>
              {showOnset && (
                <div>
                  <dt className="text-xs text-subtle">Relative to onset</dt>
                  <dd>
                    {onsetLabel(selected.onset_delta_seconds) || "Unknown"}
                  </dd>
                </div>
              )}
            </dl>
          </div>
        )}
      </Drawer>
    </>
  );
}
