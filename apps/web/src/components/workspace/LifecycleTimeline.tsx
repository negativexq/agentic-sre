import { Alert } from "@/components/ui/States";
import { Timeline, type TimelineItem } from "@/components/ui/Timeline";
import type { TimelineView } from "@/api/types";
import { dateTime, clock, humanizeSeconds } from "@/lib/format";
import { CopyButton } from "@/components/ui/CopyButton";

export function LifecycleTimeline({
  timeline,
  onset,
}: {
  timeline: TimelineView;
  onset?: string | null;
}) {
  const events: { time: string; item: TimelineItem }[] = [];
  const add = (
    time: string | null | undefined,
    label: string,
    detail: string,
    tone: TimelineItem["tone"] = "default",
  ) => {
    if (time)
      events.push({
        time,
        item: {
          label,
          detail,
          at: (
            <time dateTime={time} title={`${dateTime(time)} · ${time}`}>
              {clock(time)}
            </time>
          ),
          tone,
        },
      });
  };
  add(
    timeline.alert_fired,
    "Alert fired",
    "Recorded alert occurrence",
    "critical",
  );
  add(onset, "Incident onset", "Onset recorded by the engine");
  add(
    timeline.incident_opened,
    "Incident opened",
    "Control-plane recording time",
  );

  if (timeline.diagnosis_ready)
    add(
      timeline.diagnosis_ready,
      "Diagnosis ready",
      "Stored diagnosis time",
      "accent",
    );
  events.sort(
    (a, b) => new Date(a.time).getTime() - new Date(b.time).getTime(),
  );
  return (
    <div className="space-y-3">
      {events.length ? (
        <Timeline items={events.map((event) => event.item)} />
      ) : (
        <p className="text-sm text-muted">
          No timestamped lifecycle events recorded.
        </p>
      )}
      {timeline.phases.length > 0 && (
        <details className="border-t border-border pt-3">
          <summary className="text-xs text-accent">
            Investigation phases ({timeline.phases.length})
          </summary>
          <div className="mt-4">
            <Timeline
              items={timeline.phases.map((phase) => ({
                label: phase.name,
                at: (
                  <time dateTime={phase.at} title={phase.at}>
                    {clock(phase.at)}
                  </time>
                ),
                detail: `T+${humanizeSeconds(phase.offset_seconds)} · ${phase.detail}`,
                tone: phase.name === "Diagnosis stored" ? "accent" : "default",
              }))}
            />
          </div>
        </details>
      )}
      {timeline.note && <Alert tone="info">{timeline.note}</Alert>}
      {timeline.run_id && (
        <details className="border-t border-border pt-3">
          <summary className="text-xs text-muted">
            Run identity & recorded events
          </summary>
          <p className="mt-2 break-anywhere font-mono text-[11px] text-subtle">
            {timeline.run_id}
            <CopyButton value={timeline.run_id} label="diagnosis run" />
          </p>
          <p className="mt-1 text-xs text-muted">
            {timeline.reads} reads · {timeline.evidence_count} evidence ·{" "}
            {timeline.model_calls} model calls
          </p>
          <ul className="mt-2 space-y-1">
            {timeline.events.map((event, index) => (
              <li key={index} className="break-anywhere text-xs text-muted">
                {event.event_type} ·{" "}
                <time title={event.timestamp}>{dateTime(event.timestamp)}</time>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
