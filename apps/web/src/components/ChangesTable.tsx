import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/States";
import { TCell, THead, TRow, Table } from "@/components/ui/Table";
import type { ChangeView } from "@/api/types";
import { dateTime } from "@/lib/format";

function onsetLabel(seconds: number | null): string {
  if (seconds === null) return "";
  const abs = Math.abs(seconds);
  const unit = abs < 90 ? `${abs.toFixed(0)}s` : abs < 5400 ? `${(abs / 60).toFixed(0)}m` : `${(abs / 3600).toFixed(1)}h`;
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
    <Table>
      <THead columns={columns} />
      <tbody>
        {changes.map((change) => (
          <TRow key={change.change_id}>
            {showOnset ? (
              <TCell className="whitespace-nowrap font-mono text-xs text-subtle">
                {onsetLabel(change.onset_delta_seconds)}
              </TCell>
            ) : (
              <TCell className="whitespace-nowrap text-muted">{dateTime(change.timestamp)}</TCell>
            )}
            <TCell className="max-w-[18rem]">
              <span className="font-mono text-xs text-text break-anywhere">
                {change.resource_type}/{change.resource_name}
              </span>
              {change.matches_leading_actor && (
                <Badge tone="accent" className="ml-2">
                  leading actor
                </Badge>
              )}
            </TCell>
            <TCell>
              <Badge tone="neutral">{change.change_type}</Badge>
            </TCell>
            <TCell className="text-muted">{change.scope}</TCell>
            <TCell className="text-muted">
              {showOnset ? dateTime(change.timestamp) : (change.source ?? "—")}
            </TCell>
          </TRow>
        ))}
      </tbody>
    </Table>
  );
}
