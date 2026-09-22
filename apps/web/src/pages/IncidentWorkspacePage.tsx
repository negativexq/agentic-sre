import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { useIncident } from "@/api/hooks";
import { useLiveUpdates } from "@/api/useLiveUpdates";
import { LiveBadge } from "@/components/LiveBadge";
import type { DiagnosisView, IncidentListItem } from "@/api/types";
import { CausalPath } from "@/components/workspace/CausalPath";
import { CompetingHypotheses } from "@/components/workspace/CompetingHypotheses";
import { FindingsList } from "@/components/workspace/FindingsList";
import { LifecycleTimeline } from "@/components/workspace/LifecycleTimeline";
import { RawEvidence } from "@/components/workspace/RawEvidence";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/States";
import { TCell, THead, TRow, Table } from "@/components/ui/Table";
import { Tabs } from "@/components/ui/Tabs";
import { dateTime, shortEntity } from "@/lib/format";
import { confidenceTone, resolutionTone, severityTone } from "@/lib/tones";

function Header({ incident, live }: { incident: IncidentListItem; live: ReactNode }) {
  return (
    <div className="mb-5">
      <div className="flex items-center justify-between">
        <Link to="/incidents" className="text-sm text-accent hover:underline">
          ← All incidents
        </Link>
        {live}
      </div>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight text-text break-anywhere">
        {incident.title}
      </h1>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted">
        <Badge tone={severityTone(incident.severity)}>{incident.severity}</Badge>
        <span className="uppercase tracking-wide">{incident.status}</span>
        {incident.service && <span>· {incident.service}</span>}
        <span>· opened {dateTime(incident.created_at)}</span>
      </div>
    </div>
  );
}

function RootActor({ diagnosis }: { diagnosis: DiagnosisView }) {
  const resolved = diagnosis.is_resolved;
  const actor = diagnosis.leading_root_actor;
  return (
    <Card>
      <CardBody className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium uppercase tracking-wide text-subtle">
            {resolved ? "Root cause" : "Leading root actor"}
          </span>
          <Badge tone={confidenceTone(diagnosis.confidence)}>{diagnosis.confidence}</Badge>
          <Badge tone={resolutionTone(diagnosis.resolution)}>{diagnosis.resolution}</Badge>
        </div>
        <p className="font-mono text-lg text-text break-anywhere">
          {actor ? shortEntity(actor) : "No single root cause identified"}
        </p>
        <p className="text-sm text-muted break-anywhere">{diagnosis.summary}</p>

        {!resolved && (
          <div className="rounded-lg border border-accent/30 bg-accent-soft px-3 py-2 text-sm">
            <span className="font-medium">Ambiguous is not wrong.</span> The engine has a leading
            actor but could not positively exclude a structural alternative.
            {diagnosis.resolution_rationale && (
              <p className="mt-1 text-muted break-anywhere">{diagnosis.resolution_rationale}</p>
            )}
            {diagnosis.unresolved_dimensions.length > 0 && (
              <p className="mt-1 text-xs text-subtle">
                Unresolved: {diagnosis.unresolved_dimensions.join(", ")}
              </p>
            )}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function InvestigationTrace({ diagnosis }: { diagnosis: DiagnosisView }) {
  if (diagnosis.steps.length === 0) {
    return <p className="text-sm text-muted">No investigation steps recorded.</p>;
  }
  return (
    <Table>
      <THead columns={["Step", "Detail"]} />
      <tbody>
        {diagnosis.steps.map((step, index) => (
          <TRow key={index}>
            <TCell className="whitespace-nowrap align-top">
              <code className="text-xs text-text">{step.actor}</code>
              <span className="text-subtle"> · {step.action}</span>
            </TCell>
            <TCell className="text-muted break-anywhere">{step.detail}</TCell>
          </TRow>
        ))}
      </tbody>
    </Table>
  );
}

export function IncidentWorkspacePage() {
  const { incidentId } = useParams<{ incidentId: string }>();
  const { data, isLoading, isError, error } = useIncident(incidentId);
  const live = useLiveUpdates(incidentId ? `/incidents/${incidentId}/stream` : "/stream", [
    ["incident", incidentId],
  ]);

  if (isError) return <ErrorState message={(error as Error).message} />;
  if (isLoading || !data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32" />
        <Skeleton className="h-48" />
      </div>
    );
  }

  const { incident, diagnosis, timeline } = data;
  const failed = timeline.events.some((event) => event.event_type === "DIAGNOSIS_FAILED");

  return (
    <>
      <Header incident={incident} live={<LiveBadge state={live} />} />

      {!diagnosis ? (
        <Card>
          <CardBody>
            {failed ? (
              <>
                <p className="text-sm font-medium text-critical">Diagnosis failed.</p>
                <p className="mt-1 text-sm text-muted">
                  A diagnosis run started but did not complete. The engine records the failure; a
                  retry will open a new run. No partial result is shown.
                </p>
              </>
            ) : (
              <>
                <p className="text-sm text-text">Diagnosis in progress.</p>
                <p className="mt-1 text-sm text-muted">
                  The alert opened this incident and auto-diagnosis is running. This view updates
                  live as each phase is recorded.
                </p>
              </>
            )}
          </CardBody>
        </Card>
      ) : (
        <div className="space-y-6">
          <RootActor diagnosis={diagnosis} />

          <div className="grid gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader title="Causal path" />
              <CardBody>
                <CausalPath hops={diagnosis.causal_path} />
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="Lifecycle" />
              <CardBody>
                <LifecycleTimeline timeline={timeline} />
              </CardBody>
            </Card>
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader title="Why this actor" />
              <CardBody className="space-y-4">
                <section>
                  <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-subtle">
                    Initiating
                  </h3>
                  <FindingsList findings={diagnosis.initiating_findings} />
                </section>
                {diagnosis.supporting_findings.length > 0 && (
                  <section>
                    <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-subtle">
                      Supporting
                    </h3>
                    <FindingsList findings={diagnosis.supporting_findings} />
                  </section>
                )}
                {diagnosis.contradictory_findings.length > 0 && (
                  <section>
                    <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-subtle">
                      Contradictory
                    </h3>
                    <FindingsList findings={diagnosis.contradictory_findings} />
                  </section>
                )}
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="Competing hypotheses" />
              <CardBody>
                <CompetingHypotheses
                  hypotheses={diagnosis.competing_hypotheses}
                  candidates={diagnosis.alternatives}
                />
              </CardBody>
            </Card>
          </div>

          <Card>
            <CardHeader title="Evidence & trace" />
            <CardBody>
              <Tabs
                tabs={[
                  {
                    id: "findings",
                    label: `Evidence (${diagnosis.evidence.length})`,
                    content: <FindingsList findings={diagnosis.evidence} />,
                  },
                  {
                    id: "raw",
                    label: `Raw provenance (${data.evidence_count})`,
                    content: <RawEvidence incidentId={incident.incident_id} active />,
                  },
                  {
                    id: "trace",
                    label: `Investigation (${diagnosis.steps.length})`,
                    content: <InvestigationTrace diagnosis={diagnosis} />,
                  },
                ]}
              />
            </CardBody>
          </Card>

          <p className="text-center text-xs text-subtle">
            Mode {diagnosis.mode} · {diagnosis.model_calls} model call(s) ·{" "}
            {diagnosis.background_alerts_ignored} background alert(s) ignored
          </p>
        </div>
      )}
    </>
  );
}
