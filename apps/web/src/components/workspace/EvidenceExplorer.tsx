import { SearchInput } from "@/components/ui/Input";
import { useState, useRef } from "react";
import { useUrlFilters } from "@/lib/useUrlFilters";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { CopyButton } from "@/components/ui/CopyButton";
import type { DiagnosisView, FindingView } from "@/api/types";
import { useIncidentEvidence } from "@/api/hooks";
import { Drawer } from "@/components/ui/Drawer";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { Badge } from "@/components/ui/Badge";
import { ErrorState, Skeleton } from "@/components/ui/States";
import { FindingsList } from "./FindingsList";
import { dateTime } from "@/lib/format";
import { Button } from "@/components/ui/Button";

export function EvidenceInspector({
  finding,
  diagnosis,
  onClose,
  onOpenTurn,
  findings,
  onSelect,
  onOpenResource,
}: {
  finding: FindingView | null;
  diagnosis: DiagnosisView;
  onClose: () => void;
  onOpenTurn: (turn: number) => void;
  findings: FindingView[];
  onSelect: (finding: FindingView) => void;
  onOpenResource: (resource: string) => void;
}) {
  const pendingTurn = useRef<number | null>(null);
  const pendingResource = useRef<string | null>(null);
  const selectedIndex = findings.findIndex(
    (item) => JSON.stringify(item) === JSON.stringify(finding),
  );
  const raw = useIncidentEvidence(diagnosis.incident_id, Boolean(finding));
  const rows =
    raw.data?.filter((row) =>
      finding?.evidence_ids.includes(row.evidence_id),
    ) ?? [];
  const turns =
    diagnosis.investigation_audit?.action_audits.filter((turn) =>
      turn.returned_evidence_refs.some((id) =>
        finding?.evidence_ids.includes(id),
      ),
    ) ?? [];
  const explanations = (diagnosis.causal_explanations ?? []).filter((claim) =>
    claim.evidence_ids.some((id) => finding?.evidence_ids.includes(id)),
  );
  const questions = (diagnosis.frontier_answers ?? []).filter((question) =>
    question.evidence_ids.some((id) => finding?.evidence_ids.includes(id)),
  );
  return (
    <Drawer
      open={Boolean(finding)}
      onClose={onClose}
      title="Evidence inspector"
      onAfterClose={() => {
        if (pendingTurn.current !== null) {
          onOpenTurn(pendingTurn.current);
          pendingTurn.current = null;
        }
        if (pendingResource.current !== null) {
          onOpenResource(pendingResource.current);
          pendingResource.current = null;
        }
      }}
    >
      {finding && (
        <div className="space-y-6">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-subtle">
              {selectedIndex >= 0
                ? `Finding ${selectedIndex + 1} of ${findings.length}`
                : "Selected finding"}
            </span>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                disabled={selectedIndex <= 0}
                onClick={() => onSelect(findings[selectedIndex - 1])}
              >
                Previous finding
              </Button>
              <Button
                variant="secondary"
                disabled={
                  selectedIndex < 0 || selectedIndex >= findings.length - 1
                }
                onClick={() => onSelect(findings[selectedIndex + 1])}
              >
                Next finding
              </Button>
            </div>
          </div>
          <section>
            <p className="mb-2 text-xs uppercase tracking-wide text-subtle">
              Observation
            </p>
            <Badge>{finding.kind}</Badge>
            <p className="mt-3 break-anywhere text-sm">{finding.summary}</p>
          </section>
          <dl className="space-y-4 text-sm">
            <div>
              <dt className="text-xs text-subtle">Resource identity</dt>
              <dd className="mt-1 break-anywhere font-mono text-xs">
                {finding.entity}
                <CopyButton value={finding.entity} label="resource identity" />
                <button
                  onClick={() => {
                    pendingResource.current = finding.entity;
                    onClose();
                  }}
                  className="mt-2 block text-xs text-accent hover:underline"
                >
                  Open resource context
                </button>
              </dd>
            </div>
            <div>
              <dt className="text-xs text-subtle">Observed at</dt>
              <dd>
                {finding.at ? (
                  <time dateTime={finding.at} title={finding.at}>
                    {dateTime(finding.at)} · {finding.at}
                  </time>
                ) : (
                  "Unknown"
                )}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-subtle">Onset-relative timing</dt>
              <dd>
                {finding.onset_delta_seconds === null
                  ? "Not recorded"
                  : `${finding.onset_delta_seconds}s vs onset`}{" "}
                · {finding.temporal_role}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-subtle">Evidence IDs</dt>
              <dd className="mt-1 break-anywhere font-mono text-xs">
                {finding.evidence_ids.length
                  ? finding.evidence_ids.map((id) => (
                      <div key={id}>
                        {id} <CopyButton value={id} label="evidence ID" />
                      </div>
                    ))
                  : "No evidence references recorded"}
              </dd>
            </div>
          </dl>
          <section className="border-t border-border pt-4">
            <h3 className="mb-3 text-sm font-medium">Source provenance</h3>
            {raw.isLoading ? (
              <Skeleton className="h-24" />
            ) : raw.isError ? (
              <ErrorState message={(raw.error as Error).message} />
            ) : !rows.length ? (
              <p className="text-sm text-muted">
                No captured raw provenance matches these evidence IDs. The
                normalized finding remains available above.
              </p>
            ) : (
              rows.map((row) => (
                <div key={row.evidence_id} className="mb-4 space-y-2">
                  <p className="text-sm">
                    {row.source_system} · {row.source_type}
                  </p>
                  <p className="break-anywhere font-mono text-xs text-muted">
                    {row.evidence_id}
                  </p>
                  <p className="text-xs text-muted">
                    Collected {dateTime(row.collected_at)}
                    <br />
                    Observation window {dateTime(row.starts_at)} –{" "}
                    {dateTime(row.ends_at)}
                  </p>
                  <p className="break-anywhere text-xs text-muted">
                    Raw reference: {row.raw_result_reference}
                  </p>
                  <details>
                    <summary className="text-xs text-accent">
                      Raw observation
                    </summary>
                    <div className="mt-2">
                      <CodeBlock
                        value={JSON.stringify(row.observation, null, 2)}
                      />
                    </div>
                  </details>
                </div>
              ))
            )}
          </section>
          <section className="border-t border-border pt-4">
            <h3 className="mb-2 text-sm font-medium">Recorded relationships</h3>
            <p className="mb-3 text-xs text-muted">
              Only explicit evidence ID matches are shown. Explaining a claim
              does not establish the cause of the entire incident.
            </p>
            {explanations.map((claim, index) => (
              <div
                key={index}
                className="mb-3 rounded-md border border-border p-3 text-xs break-anywhere"
              >
                <p className="font-mono">
                  {claim.explaining_claim} → {claim.explained_claim}
                </p>
                <p className="mt-2">{claim.mechanism}</p>
                <p className="mt-1 text-muted">{claim.consequence}</p>
                <p className="mt-2 font-mono text-subtle">
                  Matched IDs:{" "}
                  {claim.evidence_ids
                    .filter((id) => finding.evidence_ids.includes(id))
                    .join(", ")}
                </p>
              </div>
            ))}
            {questions.map((question, index) => (
              <div
                key={index}
                className="mb-3 rounded-md border border-border p-3 text-xs break-anywhere"
              >
                <p>{question.question}</p>
                <p className="mt-2 text-muted">
                  {question.state} · {question.investigation_state}
                </p>
                <p className="mt-2 font-mono text-subtle">
                  Alternative: {question.alternative_id}
                </p>
                {question.remaining_uncertainty.map((uncertainty, i) => (
                  <p key={i} className="mt-1 text-muted">
                    {uncertainty}
                  </p>
                ))}
              </div>
            ))}
            {!explanations.length && !questions.length && (
              <p className="text-sm text-muted">
                No causal explanation or question explicitly references these
                evidence IDs.
              </p>
            )}
          </section>
          <section className="border-t border-border pt-4">
            <h3 className="mb-2 text-sm font-medium">
              Investigation references
            </h3>
            {turns.length ? (
              turns.map((turn) => (
                <button
                  type="button"
                  onClick={() => {
                    pendingTurn.current = turn.turn_index;
                    onClose();
                  }}
                  key={turn.turn_index}
                  className="mb-2 block rounded-md border border-border p-3 text-left break-anywhere text-xs text-accent hover:bg-surface"
                >
                  Turn {turn.turn_index} · {turn.action} ·{" "}
                  {turn.target ?? "Target not recorded"}
                  <span className="mt-1 block">Open investigation turn →</span>
                </button>
              ))
            ) : (
              <p className="text-sm text-muted">
                No investigation turn explicitly references these evidence IDs.
              </p>
            )}
          </section>
        </div>
      )}
    </Drawer>
  );
}
export function EvidenceExplorer({
  diagnosis,
  onSelect,
}: {
  diagnosis: DiagnosisView;
  onSelect: (finding: FindingView, findings: FindingView[]) => void;
}) {
  const { params, update } = useUrlFilters();
  const query = params.get("finding_q") ?? "";
  const requestedGroup = params.get("finding_group") ?? "all";
  const group = ["all", "initiating", "supporting", "contradictory"].includes(
    requestedGroup,
  )
    ? requestedGroup
    : "all";
  const [limit, setLimit] = useState(30);
  const findings =
    group === "initiating"
      ? diagnosis.initiating_findings
      : group === "supporting"
        ? diagnosis.supporting_findings
        : group === "contradictory"
          ? diagnosis.contradictory_findings
          : diagnosis.evidence;
  const filtered = findings.filter((finding) =>
    `${finding.kind} ${finding.entity} ${finding.summary} ${finding.evidence_ids.join(" ")}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs text-muted">
          Search findings
          <SearchInput
            value={query}
            onChange={(event) => {
              update({ finding_q: event.target.value });
              setLimit(30);
            }}
            placeholder="Resource, observation or evidence ID"
            className="h-10 bg-surface-raised"
          />
        </label>
      </div>
      <div className="mb-3">
        <SegmentedControl
          label="Finding group"
          value={group}
          onChange={(value) => {
            update({ finding_group: value === "all" ? null : value });
            setLimit(30);
          }}
          options={[
            {
              value: "all",
              label: "All evidence",
              count: diagnosis.evidence.length,
            },
            {
              value: "initiating",
              label: "Initiating",
              count: diagnosis.initiating_findings.length,
            },
            {
              value: "supporting",
              label: "Supporting",
              count: diagnosis.supporting_findings.length,
            },
            {
              value: "contradictory",
              label: "Contradictory",
              count: diagnosis.contradictory_findings.length,
            },
          ]}
        />
      </div>
      <p className="mb-1 text-xs text-subtle">
        {filtered.length} findings · Select an observation to inspect provenance
      </p>
      <FindingsList
        findings={filtered.slice(0, limit)}
        onSelect={(finding) => onSelect(finding, filtered)}
      />
      {filtered.length > limit && (
        <button
          className="mt-3 rounded-md border border-border px-3 py-2 text-xs text-accent"
          onClick={() => setLimit(limit + 30)}
        >
          Show 30 more findings
        </button>
      )}
    </div>
  );
}
