import { Link, useNavigate, useLocation } from "react-router-dom";
import { incidentLink } from "@/lib/incidentNavigation";

import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/States";
import { TCell, THead, TRow, Table } from "@/components/ui/Table";
import type { IncidentListItem } from "@/api/types";
import { ageFrom, dateTime, shortEntity } from "@/lib/format";
import { confidenceTone, resolutionTone, severityTone } from "@/lib/tones";

export function IncidentsTable({ items }: { items: IncidentListItem[] }) {
  const navigate = useNavigate();
  const location = useLocation();
  const href = (id: string) =>
    incidentLink(id, location.pathname, location.search);

  if (items.length === 0) {
    return (
      <EmptyState
        title="No incidents match."
        description="Alerts arrive through the Alertmanager webhook and open incidents here."
      />
    );
  }

  return (
    <Table>
      <THead
        columns={[
          "Severity",
          "Incident",
          "Service",
          "Leading root actor",
          "Confidence",
          "Resolution",
          "Age",
        ]}
      />
      <tbody>
        {items.map((item) => (
          <TRow
            key={item.incident_id}
            onClick={() => navigate(href(item.incident_id))}
          >
            <TCell>
              <Badge tone={severityTone(item.severity)}>{item.severity}</Badge>
            </TCell>
            <TCell className="max-w-[16rem]">
              <Link
                to={href(item.incident_id)}
                className="font-medium text-text hover:text-accent break-anywhere"
                onClick={(event) => event.stopPropagation()}
              >
                {item.title}
              </Link>
              <p className="mt-1 text-[11px] text-subtle">{item.status}</p>
            </TCell>
            <TCell className="max-w-[12rem] font-mono text-xs text-muted break-anywhere">
              {item.service ?? "—"}
            </TCell>
            <TCell className="max-w-[14rem]">
              {item.leading_actor_display === "COMPETING" ? (
                <span
                  className="text-xs text-muted break-anywhere"
                  title="Competing supported causes"
                >
                  Competing:{" "}
                  {(item.leading_actor_candidates ?? [])
                    .map(shortEntity)
                    .join(", ")}
                </span>
              ) : item.leading_actor_display === "NOT_ESTABLISHED" ||
                item.leading_actor_withheld_reason ? (
                <span
                  className="text-xs text-subtle"
                  title="No causal candidate is established"
                >
                  Not established
                </span>
              ) : (
                <span className="font-mono text-xs break-anywhere">
                  {shortEntity(item.leading_root_actor)}
                </span>
              )}
            </TCell>
            <TCell>
              {item.confidence ? (
                <Badge tone={confidenceTone(item.confidence)}>
                  {item.confidence}
                </Badge>
              ) : (
                <span className="text-subtle">—</span>
              )}
            </TCell>
            <TCell>
              {item.resolution ? (
                <Badge tone={resolutionTone(item.resolution)}>
                  {item.resolution}
                </Badge>
              ) : (
                <span className="text-subtle">pending</span>
              )}
            </TCell>
            <TCell className="whitespace-nowrap text-muted">
              <time dateTime={item.created_at} title={item.created_at}>
                {ageFrom(item.created_at)} ago
              </time>
              <p className="text-[10px] text-subtle">
                {dateTime(item.created_at)}
              </p>
            </TCell>
          </TRow>
        ))}
      </tbody>
    </Table>
  );
}
