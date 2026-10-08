import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { useIncident, useIncidentChanges } from "@/api/hooks";
import { useLiveUpdates } from "@/api/useLiveUpdates";
import { ChangesTable } from "@/components/ChangesTable";
import { LiveBadge } from "@/components/LiveBadge";
import { ReportExport } from "@/components/workspace/ReportExport";
import type { DiagnosisView, ExecutingInstanceView, IncidentListItem } from "@/api/types";
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
import { clock, dateTime, shortEntity } from "@/lib/format";
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

/** D3: a detail of the cause, never a second cause; an unknown time is said, not guessed. */
function executedLine(item: ExecutingInstanceView): string {
  const target = item.target ? ` on ${shortEntity(item.target)}` : "";
  const when =
    item.started_at && item.ended_at
      ? `${clock(item.started_at)}–${clock(item.ended_at)}`
      : item.started_at
        ? `from ${clock(item.started_at)}, end unknown`
        : "time unknown";
  // A standalone experiment is its own executing instance: say where it ran, not "X executed by X".
  const by = item.instance === item.actor ? "Executed" : `Executed by ${shortEntity(item.instance)}`;
  return `${by}${target}, ${when}`;
}

function instanceLine(diagnosis: DiagnosisView): string | null {
  switch (diagnosis.leader_instance_resolution) {
    case "EXACT":
      return diagnosis.leader_instance
        ? `Exact instance: ${shortEntity(diagnosis.leader_instance)}${diagnosis.leader_instance_uid ? ` (UID ${diagnosis.leader_instance_uid.slice(0, 8)})` : ""}`
        : "Exact instance determined";
    case "MULTIPLE_VIABLE":
      return "Several instances remain possible";
    case "UNKNOWN":
      return "Exact instance not determined";
    default:
      return null;
  }
}

function ExecutionDetails({ diagnosis, competing }: { diagnosis: DiagnosisView; competing: boolean }) {
  const executed = diagnosis.executing_instances ?? [];
  const instance = instanceLine(diagnosis);
  const timing = diagnosis.timing?.status;
  if (executed.length === 0 && !instance && timing !== "STABLE" && timing !== "SENSITIVE") return null;
  return (
    <ul className="space-y-0.5 text-sm text-muted">
      {executed.map((item, index) => (
        <li key={index} className="break-anywhere">
          {executedLine(item)}
          {competing && <span className="text-subtle"> · for {shortEntity(item.actor)}</span>}
        </li>
      ))}
      {instance && <li className="text-subtle">{instance}</li>}
      {timing === "STABLE" && <li className="text-subtle">Holds over every admissible onset</li>}
      {timing === "SENSITIVE" && (
        <li className="text-subtle">Depends on the exact onset (see Investigation, Timing)</li>
      )}
    </ul>
  );
}

function RootActor({ diagnosis }: { diagnosis: DiagnosisView }) {
  const resolved = diagnosis.is_resolved;
  const actor = diagnosis.leading_root_actor;
  const display =
    diagnosis.leading_actor_display ?? (diagnosis.leading_actor_withheld_reason ? "NOT_ESTABLISHED" : "SINGLE");
  const withheld = display === "NOT_ESTABLISHED";
  const competing = display === "COMPETING";
  const candidates = diagnosis.leading_actor_candidates ?? [];
  const tierLabel =
    diagnosis.leading_actor_tier === "STRONG"
      ? "Observed mechanism cause"
      : diagnosis.leading_actor_tier === "SUPPORTED"
        ? "Supported possible cause"
        : "Possible causal actor";
  return (
    <Card>
      <CardBody className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium uppercase tracking-wide text-subtle">
            {withheld ? "Causal actor" : competing ? `Competing ${diagnosis.leading_actor_tier === "STRONG" ? "observed" : "supported"} causes` : diagnosis.leading_actor_tier ? tierLabel : ["m21.v2", "m21.v3"].includes(diagnosis.decision_semantics ?? "") ? (diagnosis.claim_level === "OBSERVED_MECHANISM_CAUSE" ? "Observed mechanism cause" : "Possible causal actor") : resolved ? "Root cause" : "Leading root actor"}
          </span>
          <Badge tone={confidenceTone(diagnosis.confidence)}>{diagnosis.confidence}</Badge>
          <Badge tone={resolutionTone(diagnosis.resolution)}>{diagnosis.resolution}</Badge>
        </div>
        <p className="font-mono text-lg text-text break-anywhere">
          {withheld
            ? "Not established"
            : competing
              ? candidates.map(shortEntity).join(" · ")
              : actor
                ? shortEntity(actor)
                : "No single root cause identified"}
        </p>
        {!withheld && <ExecutionDetails diagnosis={diagnosis} competing={competing} />}
        {withheld && (
          <p className="text-sm text-muted">
            {diagnosis.leading_actor_withheld_reason === "TIED_LEADERS"
              ? `No candidate is established and several share the top rank: ${candidates.map(shortEntity).join(", ")}. None is shown as the cause.`
              : diagnosis.leading_actor_withheld_reason === "NO_EVIDENCE_NEAR_ONSET"
                ? "No candidate is established and none has evidence near the onset. The latest observation stays listed below as context, not as a cause."
                : "No candidate has evidence in the incident window. Observations outside it stay listed below as context, not as causes."}
          </p>
        )}
        <p className="text-sm text-muted break-anywhere">{diagnosis.summary}</p>
        {["m21.v2", "m21.v3"].includes(diagnosis.decision_semantics ?? "") && (
          <p className="text-sm text-muted">
            Diagnosis: {diagnosis.diagnosis_status}. Scope: {diagnosis.claim_level}.
            Incident recovery: {diagnosis.incident_recovery}.
            Context observations: {diagnosis.context_hypothesis_ids?.length ?? 0}.
            Open causal boundaries: {diagnosis.material_frontier_ids?.length ?? 0}.
            Explained claims: {diagnosis.causal_explanations?.filter(r => r.consequence === "EXPLAINS_CLAIM").length ?? 0}.
            Answered questions: {diagnosis.frontier_answers?.filter(a => a.state === "ANSWERED_ROLE_TRANSFERRED").length ?? 0}.
          </p>
        )}

        {!resolved && (
          <div className="rounded-lg border border-accent/30 bg-accent-soft px-3 py-2 text-sm">
            <span className="font-medium">Uncertainty remains.</span> The recorded evidence leaves causal distinctions open.
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

function TimingSection({ diagnosis }: { diagnosis: DiagnosisView }) {
  const timing = diagnosis.timing;
  if (!timing) return null;
  return (
    <section className="space-y-2">
      <div className="text-xs font-medium uppercase tracking-wide text-subtle">Timing</div>
      {timing.status === "UNASSESSED" ? (
        <p className="text-sm text-subtle">Timing stability not assessed for this source.</p>
      ) : (
        <p className="text-sm text-muted">
          {timing.status === "STABLE"
            ? "The decision holds over every admissible onset."
            : "The decision depends on the exact onset."}
        </p>
      )}
      {timing.withheld.length > 0 && (
        <Table>
          <THead columns={["Withheld authority", "Actor", "Relations"]} />
          <tbody>
            {timing.withheld.map((item, index) => (
              <TRow key={index}>
                <TCell>{item.authority}</TCell>
                <TCell className="break-anywhere">{item.actor}</TCell>
                <TCell className="text-muted">{item.relations.join(", ") || "—"}</TCell>
              </TRow>
            ))}
          </tbody>
        </Table>
      )}
      {timing.drivers.length > 0 && (
        <Table>
          <THead columns={["Onset", "Status there", "Actor", "Change", "Reason"]} />
          <tbody>
            {timing.drivers.map((item, index) => (
              <TRow key={index}>
                <TCell className="whitespace-nowrap">{clock(item.onset)}</TCell>
                <TCell>{item.diagnosis_status}</TCell>
                <TCell className="break-anywhere">{item.actor}</TCell>
                <TCell>{item.change}</TCell>
                <TCell className="text-muted">{item.reason}</TCell>
              </TRow>
            ))}
          </tbody>
        </Table>
      )}
    </section>
  );
}

function InvestigationTrace({ diagnosis }: { diagnosis: DiagnosisView }) {
  const audit = diagnosis.investigation_audit;
  if (diagnosis.steps.length === 0 && audit === null && !diagnosis.timing) {
    return <p className="text-sm text-muted">No investigation steps recorded.</p>;
  }
  return (
    <div className="space-y-4">
      <TimingSection diagnosis={diagnosis} />
      {diagnosis.steps.length > 0 && (
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
      )}
      {audit && (
        <section className="space-y-2">
          <div className="text-xs text-muted">
            Persisted run {audit.diagnosis_run_id} · {audit.initial_resolution} → {audit.final_resolution}
            {" · "}stop: {audit.stop_reason} · {audit.tool_calls} tool call(s)
          </div>
          {audit.action_audits.length > 0 ? (
            <Table>
              <THead columns={["Turn / observation", "Authorization", "Execution", "Evidence"]} />
              <tbody>
                {audit.action_audits.map((turn) => (
                  <TRow key={turn.turn_index}>
                    <TCell className="align-top">
                      <div>{turn.turn_index}. {turn.capability ?? turn.action} · {turn.target ?? "—"}</div>
                      <div className="text-xs text-subtle">{turn.action_rationale}</div>
                      {turn.observation_id && (
                        <div className="text-xs text-subtle">
                          {turn.observation_id} · {turn.observation_outcome ?? "no outcome"}
                        </div>
                      )}
                    </TCell>
                    <TCell className="align-top">
                      {turn.authorization_result}
                      <div className="text-xs text-subtle">{turn.authorization_reason}</div>
                    </TCell>
                    <TCell className="align-top">
                      {turn.backend_execution_status}
                      <div className="text-xs text-subtle">{turn.progress_classification}</div>
                    </TCell>
                    <TCell className="align-top break-anywhere">
                      <div>Returned: {turn.returned_evidence_refs.join(", ") || "—"}</div>
                      <div className="text-xs text-subtle">
                        New: {turn.new_evidence_refs.join(", ") || "—"}
                      </div>
                      <div className="text-xs text-subtle">
                        Known: {turn.already_known_refs.join(", ") || "—"}
                      </div>
                    </TCell>
                  </TRow>
                ))}
              </tbody>
            </Table>
          ) : (
            <p className="text-sm text-muted">No bounded actions recorded for this run.</p>
          )}
        </section>
      )}
    </div>
  );
}

function ChangesAroundOnset({ incidentId }: { incidentId: string }) {
  const { data } = useIncidentChanges(incidentId);
  return (
    <Card>
      <CardHeader
        title="Changes around onset"
        action={<span className="text-xs text-subtle">±2h window</span>}
      />
      <CardBody className="p-0">
        <ChangesTable changes={data ?? []} showOnset />
      </CardBody>
    </Card>
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

          <ChangesAroundOnset incidentId={incident.incident_id} />

          <ReportExport incidentId={incident.incident_id} />

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
