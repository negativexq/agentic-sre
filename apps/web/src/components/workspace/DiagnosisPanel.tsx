import type { ReactNode } from "react";
import { ScanLine } from "lucide-react";
import type { DiagnosisView, ExecutingInstanceView } from "@/api/types";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { clock, shortEntity } from "@/lib/format";
import { confidenceTone, resolutionTone } from "@/lib/tones";
import { CopyButton } from "@/components/ui/CopyButton";

/** D3: a detail of the cause, never a second cause; an unknown time is said, not guessed. */
function executedLine(item: ExecutingInstanceView): string {
  const target = item.target ? ` on ${item.target}` : "";
  const when =
    item.started_at && item.ended_at
      ? `${clock(item.started_at)}–${clock(item.ended_at)}`
      : item.started_at
        ? `from ${clock(item.started_at)}, end unknown`
        : item.ended_at
          ? `start unknown, until ${clock(item.ended_at)}`
          : "time unknown";
  // A standalone experiment is its own executing instance: say where it ran, not "X executed by X".
  const by =
    item.instance === item.actor ? "Executed" : `Executed by ${item.instance}`;
  return `${by}${target}, ${when}`;
}

function instanceLine(diagnosis: DiagnosisView): string | null {
  switch (diagnosis.leader_instance_resolution) {
    case "EXACT":
      return diagnosis.leader_instance
        ? `Exact instance: ${diagnosis.leader_instance}${diagnosis.leader_instance_uid ? ` (UID ${diagnosis.leader_instance_uid.slice(0, 8)})` : ""}`
        : "Exact instance determined";
    case "MULTIPLE_VIABLE":
      return "Several instances remain possible";
    case "UNKNOWN":
      return "Exact instance not determined";
    default:
      return null;
  }
}

function ExecutionDetails({
  diagnosis,
  competing,
}: {
  diagnosis: DiagnosisView;
  competing: boolean;
}) {
  const executed = (diagnosis.executing_instances ?? []).filter(
    (item) => competing || item.actor === diagnosis.leading_root_actor,
  );
  const instance =
    executed.length === 0 && !competing ? instanceLine(diagnosis) : null;
  const timing = diagnosis.timing?.status;
  if (
    executed.length === 0 &&
    !instance &&
    timing !== "STABLE" &&
    timing !== "SENSITIVE"
  )
    return null;
  return (
    <ul className="space-y-0.5 text-sm text-muted">
      {executed.map((item, index) => (
        <li key={index} className="break-anywhere">
          {executedLine(item)}
          <details className="mt-1">
            <summary className="text-xs text-accent">
              Execution identity & timestamps
            </summary>
            <p className="mt-1 break-anywhere font-mono text-[11px]">
              Instance {item.instance}
              <br />
              UID {item.instance_uid ?? "Not recorded"}
              <br />
              Target {item.target ?? "Not recorded"}
              <br />
              Started {item.started_at ?? "Time unknown"}
              <br />
              Ended {item.ended_at ?? "Time unknown"}
              <br />
              Witness rule {item.rule_id}
            </p>
          </details>
          {competing && (
            <span className="text-subtle"> · for {item.actor}</span>
          )}
        </li>
      ))}
      {instance && (
        <li className="text-subtle">
          {instance}
          {diagnosis.leader_instance_uid && (
            <details className="mt-1">
              <summary className="text-xs text-accent">
                Full instance UID
              </summary>
              <p className="mt-1 break-anywhere font-mono text-xs">
                {diagnosis.leader_instance_uid}
              </p>
            </details>
          )}
        </li>
      )}
      {timing === "STABLE" && (
        <li className="text-subtle">Holds over every admissible onset</li>
      )}
      {timing === "SENSITIVE" && (
        <li className="text-subtle">
          Depends on the exact onset (see Investigation, Timing)
        </li>
      )}
    </ul>
  );
}

export function DiagnosisPanel({
  diagnosis,
  children,
}: {
  diagnosis: DiagnosisView;
  children?: ReactNode;
}) {
  const resolved = diagnosis.resolution === "RESOLVED";
  const actor = resolved
    ? (diagnosis.root_cause ?? diagnosis.leading_root_actor)
    : diagnosis.leading_root_actor;
  const display =
    diagnosis.leading_actor_display ??
    (diagnosis.leading_actor_withheld_reason ? "NOT_ESTABLISHED" : "SINGLE");
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
    <Card className="border-accent/30">
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <ScanLine size={16} className="text-accent" aria-hidden />{" "}
            Deterministic diagnosis
          </span>
        }
        action={
          <span className="text-[11px] text-subtle">
            Engine-authored judgment
          </span>
        }
      />
      <CardBody className="space-y-4 p-5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium uppercase tracking-wide text-subtle">
            {withheld
              ? "Causal actor"
              : competing
                ? `Competing ${diagnosis.leading_actor_tier === "STRONG" ? "observed mechanism causes" : diagnosis.leading_actor_tier === "SUPPORTED" ? "supported possible causes" : "causal actors"}`
                : diagnosis.leading_actor_tier
                  ? tierLabel
                  : ["m21.v2", "m21.v3"].includes(
                        diagnosis.decision_semantics ?? "",
                      )
                    ? diagnosis.claim_level === "OBSERVED_MECHANISM_CAUSE"
                      ? "Observed mechanism cause"
                      : "Possible causal actor"
                    : resolved
                      ? "Root cause"
                      : "Leading root actor"}
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">
            <div className="inline-flex items-center gap-2">
              <span className="text-[11px] text-subtle">Confidence</span>
              <Badge tone={confidenceTone(diagnosis.confidence)}>
                {diagnosis.confidence}
              </Badge>
            </div>
            <div className="inline-flex items-center gap-2">
              <span className="text-[11px] text-subtle">Resolution</span>
              <Badge tone={resolutionTone(diagnosis.resolution)}>
                {diagnosis.resolution}
              </Badge>
            </div>
          </div>
        </div>
        <p className="font-mono text-lg font-medium sm:text-2xl tracking-tight text-text break-anywhere">
          {withheld
            ? "Not established"
            : competing
              ? `${candidates.length} competing actors`
              : actor
                ? actor
                : "No single root cause identified"}
          {!withheld && !competing && actor && (
            <CopyButton value={actor} label="causal actor identity" />
          )}
        </p>
        {!withheld && !competing && (
          <ExecutionDetails diagnosis={diagnosis} competing={false} />
        )}
        {competing &&
          candidates.map((candidate) => (
            <div
              key={candidate}
              className="rounded-md border border-border bg-surface p-3"
            >
              <p className="mb-2 break-anywhere font-mono text-sm">
                {candidate}
                <CopyButton value={candidate} label="candidate identity" />
              </p>
              <ExecutionDetails
                competing
                diagnosis={{
                  ...diagnosis,
                  executing_instances: (
                    diagnosis.executing_instances ?? []
                  ).filter((item) => item.actor === candidate),
                  leader_instance_resolution: null,
                  timing: null,
                }}
              />
            </div>
          ))}
        {withheld && (
          <p className="text-sm text-muted">
            {diagnosis.leading_actor_withheld_reason === "TIED_LEADERS"
              ? `No candidate is established and several share the top rank: ${candidates.map(shortEntity).join(", ")}. None is shown as the cause.`
              : diagnosis.leading_actor_withheld_reason ===
                  "NO_EVIDENCE_NEAR_ONSET"
                ? "No candidate is established and none has evidence near the onset. The latest observation stays listed below as context, not as a cause."
                : `The engine has not established a causal actor.${diagnosis.leading_actor_withheld_reason ? ` Withheld reason: ${diagnosis.leading_actor_withheld_reason}.` : ""}`}
          </p>
        )}
        <p className="text-sm text-muted break-anywhere">{diagnosis.summary}</p>
        {(diagnosis.diagnosis_status ||
          diagnosis.claim_level ||
          diagnosis.incident_recovery) && (
          <dl className="grid gap-4 border-t border-border pt-3 sm:grid-cols-3">
            {[
              ["Diagnosis status", diagnosis.diagnosis_status],
              ["Claim level", diagnosis.claim_level],
              ["Incident recovery", diagnosis.incident_recovery],
            ].map(([label, value]) =>
              value ? (
                <div key={label} className="min-w-0">
                  <dt className="text-[11px] text-subtle">{label}</dt>
                  <dd className="mt-1 break-anywhere text-xs text-muted">
                    {value}
                  </dd>
                </div>
              ) : null,
            )}
          </dl>
        )}
        {competing && diagnosis.timing?.status === "STABLE" && (
          <p className="text-xs text-muted">
            Holds over every admissible onset
          </p>
        )}
        {competing && diagnosis.timing?.status === "SENSITIVE" && (
          <p className="text-xs text-muted">
            Depends on the exact onset (see Investigation, Timing)
          </p>
        )}
        {diagnosis.causal_explanation && (
          <details className="border-t border-border pt-3">
            <summary className="text-xs text-accent">
              Engine causal explanation
            </summary>
            <p className="mt-2 break-anywhere text-sm text-muted">
              {diagnosis.causal_explanation}
            </p>
          </details>
        )}
        {!resolved && (
          <div className="rounded-lg border border-accent/30 bg-accent-soft px-3 py-2 text-sm">
            <span className="font-medium">Uncertainty remains.</span> The
            recorded evidence leaves causal distinctions open.
            {diagnosis.resolution_rationale && (
              <p className="mt-1 text-muted break-anywhere">
                {diagnosis.resolution_rationale}
              </p>
            )}
            {diagnosis.unresolved_dimensions.length > 0 && (
              <p className="mt-1 text-xs text-subtle">
                Unresolved: {diagnosis.unresolved_dimensions.join(", ")}
              </p>
            )}
          </div>
        )}
      </CardBody>
      {children}
    </Card>
  );
}
