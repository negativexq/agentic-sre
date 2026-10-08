import { Link } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import type { IncidentListItem } from "@/api/types";
import { Badge } from "@/components/ui/Badge";
import { confidenceTone, resolutionTone } from "@/lib/tones";
import { ageFrom, shortEntity } from "@/lib/format";
export function RecentDiagnoses({ items }: { items: IncidentListItem[] }) {
  if (!items.length)
    return <p className="py-3 text-sm text-muted">No diagnoses recorded.</p>;
  return (
    <ol className="divide-y divide-border">
      {items.map((item) => (
        <li key={item.incident_id} className="py-4 first:pt-0 last:pb-0">
          <Link
            to={`/incidents/${item.incident_id}`}
            className="group flex items-start justify-between gap-3"
          >
            <span className="break-anywhere text-sm font-medium group-hover:text-accent">
              {item.title}
            </span>
            <ArrowUpRight
              size={14}
              className="mt-1 shrink-0 text-subtle"
              aria-hidden
            />
          </Link>
          <p className="mt-2 break-anywhere font-mono text-[11px] text-muted">
            {item.leading_actor_display === "COMPETING"
              ? `Competing: ${(item.leading_actor_candidates ?? []).map(shortEntity).join(" · ")}`
              : item.leading_actor_display === "NOT_ESTABLISHED" ||
                  item.leading_actor_withheld_reason
                ? "Causal actor not established"
                : shortEntity(item.leading_root_actor)}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            {item.confidence && (
              <Badge tone={confidenceTone(item.confidence)}>
                {item.confidence}
              </Badge>
            )}
            {item.resolution && (
              <Badge tone={resolutionTone(item.resolution)}>
                {item.resolution}
              </Badge>
            )}
          </div>
          <p className="mt-2 text-[10px] text-subtle">
            Incident opened{" "}
            <time dateTime={item.created_at} title={item.created_at}>
              {ageFrom(item.created_at)} ago
            </time>
            {item.service && ` · ${item.service}`}
          </p>
        </li>
      ))}
    </ol>
  );
}
