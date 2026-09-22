import { Alert } from "@/components/ui/States";
import { Timeline, type TimelineItem } from "@/components/ui/Timeline";
import type { TimelineView } from "@/api/types";
import { clock, humanizeSeconds } from "@/lib/format";

export function LifecycleTimeline({ timeline }: { timeline: TimelineView }) {
  const items: TimelineItem[] = [];

  if (timeline.alert_fired) {
    items.push({
      label: "Alert fired",
      at: clock(timeline.alert_fired),
      detail: "Prometheus threshold breached",
      tone: "critical",
    });
  }
  if (timeline.incident_opened) {
    items.push({
      label: "Incident opened",
      at: clock(timeline.incident_opened),
      detail: "Alertmanager → control-plane webhook",
    });
  }
  for (const phase of timeline.phases) {
    items.push({
      label: phase.name,
      at: clock(phase.at),
      detail: `T+${humanizeSeconds(phase.offset_seconds)} · ${phase.detail}`,
      tone: phase.name === "Diagnosis stored" ? "accent" : "default",
    });
  }

  return (
    <div className="space-y-3">
      {items.length > 0 ? <Timeline items={items} /> : null}
      {timeline.phases.length === 0 && !timeline.note && (
        <p className="text-sm text-muted">Auto-diagnosis running…</p>
      )}
      {timeline.note && <Alert tone="info">{timeline.note}</Alert>}
    </div>
  );
}
