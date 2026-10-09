import type {
  DiagnosisView,
  FindingView,
  IncidentListItem,
  TimelineView,
} from "@/api/types";
import { Activity, CircleHelp, Fingerprint } from "lucide-react";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { DiagnosisPanel } from "./DiagnosisPanel";
import { CausalPath } from "./CausalPath";
import { FindingsList } from "./FindingsList";
import { CompetingHypotheses } from "./CompetingHypotheses";
import { LifecycleTimeline } from "./LifecycleTimeline";
import { CopyButton } from "@/components/ui/CopyButton";
import { WhyNotResolved } from "./WhyNotResolved";
import { RemediationProposal } from "./RemediationProposal";
import { useInvestigationCanvas } from "@/components/investigation/context";

export function DiagnosisOverview({
  incident,
  diagnosis,
  timeline,
  onSelect,
  onOpenHypothesis,
  onOpenResource,
}: {
  incident: IncidentListItem;
  diagnosis: DiagnosisView;
  timeline: TimelineView;
  onSelect: (finding: FindingView, findings?: FindingView[]) => void;
  onOpenHypothesis: (id: string) => void;
  onOpenResource: (resource: string) => void;
}) {
  const canvas = useInvestigationCanvas();
  return (
    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_340px] 2xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="min-w-0 space-y-5">
        <DiagnosisPanel diagnosis={diagnosis}>
          <div className="border-t border-border bg-surface/40 p-5">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-muted">
                Engine causal path
              </h2>
              <span className="text-[11px] text-subtle">
                {diagnosis.causal_path.length} recorded hops
              </span>
            </div>
            <CausalPath
              hops={diagnosis.causal_path}
              onOpenResource={(resource) => {
                const findings = diagnosis.evidence.filter(
                  (item) => item.entity === resource,
                );
                if (findings.length) onSelect(findings[0], findings);
                else onOpenResource(resource);
                canvas?.select({ kind: "resource", id: resource });
              }}
            />
          </div>
        </DiagnosisPanel>
        <WhyNotResolved
          diagnosis={diagnosis}
          onSelect={onSelect}
          onOpenHypothesis={onOpenHypothesis}
        />
        <section className="overflow-hidden rounded-lg border border-border bg-surface-raised">
          <div className="flex items-center justify-between border-b border-border px-5 py-4">
            <h2 className="text-sm font-semibold">Evidence basis</h2>
            <span className="text-[11px] text-subtle">
              Select a finding to inspect
            </span>
          </div>
          <div className="px-4 py-2">
            {[
              { label: "Initiating", findings: diagnosis.initiating_findings },
              { label: "Supporting", findings: diagnosis.supporting_findings },
              {
                label: "Contradictory",
                findings: diagnosis.contradictory_findings,
              },
            ]
              .filter(
                (group) =>
                  group.label === "Initiating" || group.findings.length > 0,
              )
              .map(({ label, findings }) => (
                <section key={label} className="py-2">
                  <h3 className="px-2 text-[10px] font-semibold uppercase tracking-widest text-subtle">
                    {label} · {findings.length}
                  </h3>
                  <FindingsList
                    findings={findings.slice(0, 3)}
                    onSelect={onSelect}
                  />
                  {findings.length > 3 && (
                    <p className="px-2 text-xs text-muted">
                      All {findings.length} available in the Evidence view
                    </p>
                  )}
                </section>
              ))}
          </div>
        </section>
        <RemediationProposal proposals={diagnosis.remediation} />
      </div>
      <aside
        className="min-w-0 space-y-4"
        aria-label="Incident context and alternatives"
      >
        <Card>
          <CardHeader
            title={
              <span className="flex items-center gap-2">
                <CircleHelp size={15} className="text-subtle" aria-hidden />
                Alternative explanations
              </span>
            }
          />
          <CardBody>
            <CompetingHypotheses
              hypotheses={diagnosis.competing_hypotheses}
              candidates={diagnosis.alternatives}
            />
          </CardBody>
        </Card>
        <Card>
          <CardHeader
            title={
              <span className="flex items-center gap-2">
                <Activity size={15} className="text-subtle" aria-hidden />
                Recorded lifecycle
              </span>
            }
          />
          <CardBody>
            <LifecycleTimeline timeline={timeline} onset={diagnosis.onset} />
          </CardBody>
        </Card>
        <Card>
          <CardHeader
            title={
              <span className="flex items-center gap-2">
                <Fingerprint size={15} className="text-subtle" aria-hidden />
                Case context
              </span>
            }
          />
          <CardBody>
            <dl className="space-y-4 text-xs">
              <div>
                <dt className="text-subtle">Affected services</dt>
                <dd className="mt-1 break-anywhere font-mono">
                  {diagnosis.services.join(" · ") ||
                    incident.service ||
                    "Not recorded"}
                </dd>
              </div>
              <div>
                <dt className="text-subtle">Alerts</dt>
                <dd className="mt-1 break-anywhere font-mono">
                  {diagnosis.alert_names.join(" · ") || "Not recorded"}
                </dd>
              </div>
              <div>
                <dt className="text-subtle">Incident ID</dt>
                <dd className="mt-1 break-anywhere font-mono">
                  {incident.incident_id}
                  <CopyButton
                    value={incident.incident_id}
                    label="incident ID"
                  />
                </dd>
              </div>
              <div>
                <dt className="text-subtle">Source</dt>
                <dd className="mt-1">{incident.source}</dd>
              </div>
            </dl>
          </CardBody>
        </Card>
      </aside>
    </div>
  );
}
