import { useNavigate } from "react-router-dom";

import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/States";
import { TCell, THead, TRow, Table } from "@/components/ui/Table";
import type { IncidentListItem } from "@/api/types";
import { ageFrom, shortEntity } from "@/lib/format";
import { confidenceTone, resolutionTone, severityTone } from "@/lib/tones";

export function IncidentsTable({ items }: { items: IncidentListItem[] }) {
  const navigate = useNavigate();

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
          <TRow key={item.incident_id} onClick={() => navigate(`/incidents/${item.incident_id}`)}>
            <TCell>
              <Badge tone={severityTone(item.severity)}>{item.severity}</Badge>
            </TCell>
            <TCell className="max-w-[16rem]">
              <span className="font-medium text-text break-anywhere">{item.title}</span>
            </TCell>
            <TCell className="text-muted">{item.service ?? "—"}</TCell>
            <TCell className="max-w-[14rem]">
              <span className="font-mono text-xs break-anywhere">
                {shortEntity(item.leading_root_actor)}
              </span>
            </TCell>
            <TCell>
              {item.confidence ? (
                <Badge tone={confidenceTone(item.confidence)}>{item.confidence}</Badge>
              ) : (
                <span className="text-subtle">—</span>
              )}
            </TCell>
            <TCell>
              {item.resolution ? (
                <Badge tone={resolutionTone(item.resolution)}>{item.resolution}</Badge>
              ) : (
                <span className="text-subtle">pending</span>
              )}
            </TCell>
            <TCell className="whitespace-nowrap text-muted">{ageFrom(item.created_at)}</TCell>
          </TRow>
        ))}
      </tbody>
    </Table>
  );
}
