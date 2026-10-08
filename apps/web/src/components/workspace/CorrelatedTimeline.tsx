import { useState } from "react";
import type { ChangeView, TimelineView } from "@/api/types";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { dateTime } from "@/lib/format";

type Entry = {
  id: string;
  at: string | null;
  kind: string;
  label: string;
  detail: string;
  timeMeaning: string;
};

/** Chronology of explicit timestamps, not causal edges or an inferred execution order. */
export function CorrelatedTimeline({
  timeline,
  onset,
  changes,
}: {
  timeline: TimelineView;
  onset: string | null;
  changes: ChangeView[];
}) {
  const [limit, setLimit] = useState(40);
  const entries: Entry[] = [
    {
      id: "alert",
      at: timeline.alert_fired,
      kind: "Alert",
      label: "Alert fired",
      detail: "",
      timeMeaning: "Alert occurrence",
    },
    {
      id: "onset",
      at: onset,
      kind: "Onset",
      label: "Incident onset",
      detail: "",
      timeMeaning: "Engine-recorded onset",
    },
    {
      id: "opened",
      at: timeline.incident_opened,
      kind: "Incident",
      label: "Incident opened",
      detail: "",
      timeMeaning: "Control-plane recording time",
    },
    {
      id: "ready",
      at: timeline.diagnosis_ready,
      kind: "Diagnosis",
      label: "Diagnosis ready",
      detail: "",
      timeMeaning: "Stored diagnosis time",
    },
    ...timeline.phases.map((phase, index) => ({
      id: `phase-${index}`,
      at: phase.at,
      kind: "Phase",
      label: phase.name,
      detail: phase.detail,
      timeMeaning: "Recorded phase time",
    })),
    ...timeline.events.map((event, index) => ({
      id: `event-${index}`,
      at: event.timestamp,
      kind: "Event",
      label: event.event_type,
      detail: "",
      timeMeaning: "Recorded event timestamp",
    })),
    ...changes.map((change) => ({
      id: `change-${change.change_id}`,
      at: change.timestamp,
      kind: "Change",
      label: `${change.resource_type}/${change.resource_name}`,
      detail: `${change.change_type} · ${change.scope} · ${change.source ?? "Source not recorded"}`,
      timeMeaning: "Change occurrence",
    })),
  ];
  const known = entries
    .filter((entry) => entry.at && Number.isFinite(Date.parse(entry.at)))
    .sort((a, b) => Date.parse(a.at!) - Date.parse(b.at!));
  const unknown = entries.filter(
    (entry) => !entry.at || !Number.isFinite(Date.parse(entry.at)),
  );
  const groups = new Map<number, Entry[]>();
  for (const entry of known.slice(0, limit)) {
    const time = Date.parse(entry.at!);
    groups.set(time, [...(groups.get(time) ?? []), entry]);
  }
  const row = (entry: Entry) => (
    <li key={entry.id} className="min-w-0 py-2.5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <Badge>{entry.kind}</Badge>
        <span className="min-w-0 flex-1 basis-40 break-anywhere text-sm font-medium">
          {entry.label}
        </span>
        <span className="text-[11px] text-subtle">{entry.timeMeaning}</span>
      </div>
      {entry.detail && (
        <p className="mt-1 break-anywhere text-xs text-muted">{entry.detail}</p>
      )}
    </li>
  );
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted">
        Recorded chronology · changes from 2h before to 30m after onset. Temporal proximity
        does not establish causation. Entries at the same timestamp are grouped
        without implying an order within that group.
      </p>
      <ol
        aria-label="Timestamped investigation chronology"
        className="divide-y divide-border border-l border-border pl-4"
      >
        {[...groups].map(([time, group]) => (
          <li
            key={time}
            className="grid gap-x-5 py-3 sm:grid-cols-[210px_minmax(0,1fr)]"
          >
            <time
              dateTime={group[0].at!}
              title={group[0].at!}
              className="break-anywhere pt-2.5 font-mono text-xs text-subtle"
            >
              {dateTime(group[0].at!)}
              <span className="mt-1 block text-[10px]">
                {new Date(time).toISOString()}
              </span>
            </time>
            <ul className="min-w-0 divide-y divide-border/60">
              {group.map(row)}
            </ul>
          </li>
        ))}
      </ol>
      {!known.length && (
        <p className="text-sm text-muted">
          No timestamped observations recorded.
        </p>
      )}
      {known.length > limit && (
        <Button variant="secondary" onClick={() => setLimit(limit + 40)}>
          Show 40 more records
        </Button>
      )}
      {unknown.length > 0 && (
        <section className="border-t border-border pt-4">
          <h3 className="mb-2 text-sm font-medium">Time not established</h3>
          <p className="mb-3 text-xs text-muted">
            These records are excluded from chronological ordering.
          </p>
          <ul className="divide-y divide-border">{unknown.map(row)}</ul>
        </section>
      )}
      {timeline.note && <p className="text-xs text-muted">{timeline.note}</p>}
    </div>
  );
}
