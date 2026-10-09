import { useState, type ReactNode } from "react";
import {
  ArrowLeft,
  ScanLine,
  Layers,
  History,
  FileText,
  Search,
  GitBranch,
} from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useIncident, useIncidentChanges } from "@/api/hooks";
import { useLiveUpdates } from "@/api/useLiveUpdates";
import type { IncidentListItem, FindingView } from "@/api/types";
import { LiveBadge } from "@/components/LiveBadge";
import { ChangesTable } from "@/components/ChangesTable";
import { DiagnosisOverview } from "@/components/workspace/DiagnosisOverview";
import { TimingSection } from "@/components/workspace/TimingSection";
import { LifecycleTimeline } from "@/components/workspace/LifecycleTimeline";
import {
  EvidenceExplorer,
  EvidenceInspector,
} from "@/components/workspace/EvidenceExplorer";
import { InvestigationHistory } from "@/components/workspace/InvestigationHistory";
import { CausalBoundaries } from "@/components/workspace/CausalBoundaries";
import { RawEvidenceDisclosure } from "@/components/workspace/RawEvidence";
import { ReportExport } from "@/components/workspace/ReportExport";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorState, Skeleton } from "@/components/ui/States";
import { Tabs } from "@/components/ui/Tabs";
import { dateTime } from "@/lib/format";
import { DiagnosisHistory } from "@/components/workspace/DiagnosisHistory";
import { EvidenceCoverage } from "@/components/workspace/EvidenceCoverage";
import { ResourceInspector } from "@/components/workspace/ResourceInspector";
import { InvestigationReplay } from "@/components/workspace/InvestigationReplay";
import { Button } from "@/components/ui/Button";
import { HypothesisInspector } from "@/components/workspace/HypothesisInspector";
import { CorrelatedTimeline } from "@/components/workspace/CorrelatedTimeline";
import { incidentReturn } from "@/lib/incidentNavigation";
import { severityTone } from "@/lib/tones";
import { useUrlFilters } from "@/lib/useUrlFilters";
import {
  InvestigationCanvas,
  InvestigationCanvasProvider,
} from "@/components/investigation/InvestigationCanvas";
import { findingKey } from "@/components/investigation/selection";

function Header({
  incident,
  onset,
  live,
  backTo,
}: {
  incident: IncidentListItem;
  onset?: string | null;
  live: ReactNode;
  backTo: string;
}) {
  return (
    <div className="mb-5">
      <div className="flex items-center justify-between">
        <Link
          to={backTo}
          className="inline-flex items-center gap-2 text-xs text-muted hover:text-accent"
        >
          <ArrowLeft size={14} aria-hidden /> All incidents
        </Link>
        {live}
      </div>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight text-text break-anywhere">
        {incident.title}
      </h1>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted">
        <Badge tone={severityTone(incident.severity)}>
          {incident.severity}
        </Badge>
        <span className="uppercase tracking-wide">{incident.status}</span>
        {incident.service && (
          <span className="break-anywhere font-mono text-xs">
            · {incident.service}
          </span>
        )}
        <span>
          · opened{" "}
          <time dateTime={incident.created_at} title={incident.created_at}>
            {dateTime(incident.created_at)}
          </time>
        </span>
        {onset && (
          <span>
            · onset{" "}
            <time dateTime={onset} title={onset}>
              {dateTime(onset)}
            </time>
          </span>
        )}
      </div>
    </div>
  );
}

export function IncidentWorkspacePage() {
  const [selected, setSelected] = useState<FindingView | null>(null);
  const [returnFocusLabel, setReturnFocusLabel] = useState<string | null>(null);
  const [selectedFindings, setSelectedFindings] = useState<FindingView[]>([]);
  const { params, update } = useUrlFilters();
  const requestedTab = params.get("tab") ?? "diagnosis";
  const tab = [
    "diagnosis",
    "evidence",
    "timeline",
    "boundaries",
    "reports",
    "revisions",
    "trace",
  ].includes(requestedTab)
    ? requestedTab
    : "diagnosis";
  const { incidentId } = useParams<{ incidentId: string }>();
  const changes = useIncidentChanges(incidentId);
  const { data, isLoading, isError, error } = useIncident(incidentId);
  const live = useLiveUpdates(
    incidentId ? `/incidents/${incidentId}/stream` : "/stream",
    [
      ["incident", incidentId],
      ["incident-changes", incidentId],
      ["incident-evidence", incidentId],
      ["diagnosis-revisions", incidentId],
      ["evidence-coverage", incidentId],
    ],
  );
  if (isError && !data)
    return <ErrorState message={(error as Error).message} />;
  if (isLoading || !data)
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32" />
        <Skeleton className="h-48" />
      </div>
    );
  const { incident, diagnosis, timeline } = data;
  const selectFinding = (finding: FindingView, findings?: FindingView[]) => {
    if (!selected)
      setReturnFocusLabel(
        document.activeElement?.getAttribute("aria-label") ?? null,
      );
    setSelected(finding);
    setSelectedFindings(findings ?? diagnosis?.evidence ?? [finding]);
    if (
      !["resource", "change", "turn"].includes(params.get("canvas_kind") ?? "")
    )
      update({ canvas_kind: "finding", canvas_id: findingKey(finding) });
  };
  const started =
    timeline.phases.length > 0 ||
    timeline.events.some((event) => event.event_type === "DIAGNOSIS_STARTED");
  const failed = timeline.events.some(
    (event) => event.event_type === "DIAGNOSIS_FAILED",
  );
  return (
    <>
      <Header
        incident={incident}
        backTo={incidentReturn(params.get("returnTo"))}
        onset={diagnosis?.onset}
        live={<LiveBadge state={live} />}
      />
      {isError && (
        <ErrorState
          message={`Showing the last loaded incident. Refresh failed: ${(error as Error).message}`}
        />
      )}
      {!diagnosis ? (
        <Card>
          <CardBody>
            <p
              className={`text-sm font-medium ${failed ? "text-critical" : "text-text"}`}
            >
              {failed
                ? "Diagnosis failed."
                : started
                  ? "Diagnosis in progress."
                  : "Awaiting diagnosis."}
            </p>
            <p className="mt-2 text-sm text-muted">
              {failed
                ? "A recorded diagnosis run did not complete. No partial result is shown."
                : started
                  ? "A diagnosis run has started. This view updates as its phases are recorded."
                  : "No diagnosis has been stored. This view updates when the control plane records investigation activity."}
            </p>
            <div className="mt-5">
              <LifecycleTimeline timeline={timeline} />
            </div>
          </CardBody>
        </Card>
      ) : (
        <InvestigationCanvasProvider
          diagnosis={diagnosis}
          changes={changes.data ?? []}
        >
          <InvestigationCanvas
            diagnosis={diagnosis}
            onSelect={selectFinding}
            onOpenResource={(resource) => update({ resource })}
            onOpenHypothesis={(hypothesis) => update({ hypothesis })}
          />
          <Tabs
            initial="diagnosis"
            value={tab}
            onChange={(value) =>
              update({ tab: value === "diagnosis" ? null : value, turn: null })
            }
            tabs={[
              {
                id: "diagnosis",
                label: (
                  <>
                    <ScanLine size={15} aria-hidden />
                    Diagnosis
                  </>
                ),
                content: (
                  <DiagnosisOverview
                    incident={incident}
                    diagnosis={diagnosis}
                    timeline={timeline}
                    onSelect={selectFinding}
                    onOpenHypothesis={(id) => update({ hypothesis: id })}
                    onOpenResource={(resource) =>
                      update({
                        resource,
                        canvas_kind: "resource",
                        canvas_id: resource,
                      })
                    }
                  />
                ),
              },
              {
                id: "evidence",
                label: (
                  <>
                    <Search size={15} aria-hidden />
                    Evidence{" "}
                    <span className="rounded bg-unverified-soft px-1.5 font-mono text-[10px]">
                      {diagnosis.evidence.length}
                    </span>
                  </>
                ),
                content: (
                  <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_300px]">
                    <Card>
                      <CardHeader title="Evidence explorer" />
                      <CardBody>
                        <EvidenceExplorer
                          diagnosis={diagnosis}
                          onSelect={selectFinding}
                        />
                      </CardBody>
                    </Card>
                    <aside className="space-y-4">
                      <Card>
                        <CardHeader title="Provenance" />
                        <CardBody>
                          <p className="text-sm text-muted">
                            Findings are normalized deterministic signals.
                            Select one to inspect its explicitly referenced
                            source observations.
                          </p>
                          <p className="mt-3 text-xs text-subtle">
                            {data.evidence_count} raw evidence rows recorded
                          </p>
                        </CardBody>
                      </Card>
                      <Card>
                        <CardHeader title="Observation coverage" />
                        <CardBody>
                          <EvidenceCoverage incidentId={incident.incident_id} />
                        </CardBody>
                      </Card>
                      <RawEvidenceDisclosure
                        incidentId={incident.incident_id}
                        count={data.evidence_count}
                      />
                    </aside>
                  </div>
                ),
              },
              {
                id: "timeline",
                label: (
                  <>
                    <History size={15} aria-hidden />
                    Timeline & changes
                  </>
                ),
                content: (
                  <div className="space-y-4">
                    <Card>
                      <CardHeader title="Recorded incident chronology" />
                      <CardBody>
                        {changes.isLoading && (
                          <p className="mb-3 text-xs text-subtle">
                            Loading recorded changes…
                          </p>
                        )}
                        {changes.isError && (
                          <ErrorState
                            message={`Changes could not be loaded: ${(changes.error as Error).message}`}
                          />
                        )}
                        <CorrelatedTimeline
                          timeline={timeline}
                          onset={diagnosis.onset}
                          changes={changes.data ?? []}
                          findings={diagnosis.evidence}
                        />
                      </CardBody>
                    </Card>
                    <details className="rounded-lg border border-border p-4">
                      <summary className="text-sm font-medium">
                        Lifecycle details & change inspection
                      </summary>
                      <div className="mt-4">
                        <div className="grid items-start gap-5 xl:grid-cols-[340px_minmax(0,1fr)]">
                          <Card>
                            <CardHeader title="Recorded lifecycle" />
                            <CardBody>
                              <LifecycleTimeline
                                timeline={timeline}
                                onset={diagnosis.onset}
                              />
                            </CardBody>
                          </Card>
                          <Card>
                            <CardHeader
                              title="Changes around onset"
                              action={
                                <span className="text-xs text-subtle">
                                  −2h / +30m window
                                </span>
                              }
                            />
                            <CardBody className="p-0">
                              <p className="border-b border-border p-4 text-xs text-muted">
                                Temporal proximity alone does not establish
                                causation.
                              </p>
                              {changes.isError ? (
                                <ErrorState
                                  message={(changes.error as Error).message}
                                />
                              ) : changes.isLoading ? (
                                <Skeleton className="h-32" />
                              ) : (
                                <ChangesTable
                                  changes={changes.data ?? []}
                                  showOnset
                                />
                              )}
                            </CardBody>
                          </Card>
                        </div>
                      </div>
                    </details>
                  </div>
                ),
              },
              {
                id: "boundaries",
                label: (
                  <>
                    <GitBranch size={15} aria-hidden />
                    Causal boundaries
                  </>
                ),
                content: (
                  <Card>
                    <CardHeader title="Claims & remaining questions" />
                    <CardBody>
                      <CausalBoundaries
                        diagnosis={diagnosis}
                        onSelect={selectFinding}
                      />
                    </CardBody>
                  </Card>
                ),
              },
              {
                id: "reports",
                label: (
                  <>
                    <FileText size={15} aria-hidden />
                    Reports
                  </>
                ),
                content: <ReportExport incidentId={incident.incident_id} />,
              },
              {
                id: "revisions",
                label: (
                  <>
                    <History size={15} aria-hidden />
                    Diagnosis history
                  </>
                ),
                content: <DiagnosisHistory incidentId={incident.incident_id} />,
              },
              {
                id: "trace",
                label: (
                  <>
                    <Layers size={15} aria-hidden />
                    Investigation{" "}
                    <span className="rounded bg-unverified-soft px-1.5 font-mono text-[10px]">
                      {diagnosis.investigation_audit?.action_audits.length ??
                        diagnosis.steps.length}
                    </span>
                  </>
                ),
                content: (
                  <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
                    <Card>
                      <CardHeader title="Bounded investigation" />
                      <CardBody>
                        <div
                          className="mb-4 flex flex-wrap gap-2"
                          role="group"
                          aria-label="Investigation display"
                        >
                          <Button
                            variant={
                              params.get("investigation_view") !== "recorded"
                                ? "secondary"
                                : "ghost"
                            }
                            aria-pressed={
                              params.get("investigation_view") !== "recorded"
                            }
                            onClick={() => update({ investigation_view: null })}
                          >
                            History
                          </Button>
                          <Button
                            variant={
                              params.get("investigation_view") === "recorded"
                                ? "secondary"
                                : "ghost"
                            }
                            aria-pressed={
                              params.get("investigation_view") === "recorded"
                            }
                            onClick={() =>
                              update({
                                investigation_view: "recorded",
                                canvas_kind: "turn",
                                canvas_id:
                                  params.get("replay_turn") ??
                                  String(
                                    diagnosis.investigation_audit
                                      ?.action_audits[0]?.turn_index ?? "",
                                  ),
                              })
                            }
                          >
                            Recorded investigation
                          </Button>
                        </div>
                        {params.get("investigation_view") === "recorded" ? (
                          <InvestigationReplay
                            diagnosis={diagnosis}
                            onOpenHypothesis={(id) =>
                              update({ hypothesis: id })
                            }
                          />
                        ) : (
                          <InvestigationHistory
                            diagnosis={diagnosis}
                            onOpenHypothesis={(id) =>
                              update({ hypothesis: id })
                            }
                          />
                        )}
                      </CardBody>
                    </Card>
                    <Card>
                      <CardHeader title="Timing & authority" />
                      <CardBody>
                        <TimingSection diagnosis={diagnosis} />
                      </CardBody>
                    </Card>
                  </div>
                ),
              },
            ]}
          />
          <ResourceInspector
            resource={params.get("resource")}
            diagnosis={diagnosis}
            onClose={() => update({ resource: null })}
            onSelect={selectFinding}
          />
          <HypothesisInspector
            id={params.get("hypothesis")}
            hypotheses={diagnosis.competing_hypotheses}
            onClose={() => update({ hypothesis: null })}
          />
          <EvidenceInspector
            restoreFocusLabel={returnFocusLabel}
            finding={selected}
            diagnosis={diagnosis}
            onClose={() => setSelected(null)}
            findings={selectedFindings}
            onSelect={(finding) => {
              selectFinding(finding, selectedFindings);
            }}
            onOpenResource={(resource) => update({ resource })}
            onOpenTurn={(turn) =>
              update({
                tab: "trace",
                turn: String(turn),
                audit_filter: null,
                investigation_view: null,
                canvas_kind: "turn",
                canvas_id: String(turn),
              })
            }
          />
          <footer className="mt-6 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3 text-[11px] text-subtle">
            <span>Deterministic judgment · Read-only investigation</span>
            <span>
              Mode {diagnosis.mode} · {diagnosis.model_calls} model calls ·{" "}
              {diagnosis.background_alerts_ignored} background alerts ignored
            </span>
          </footer>
        </InvestigationCanvasProvider>
      )}
    </>
  );
}
