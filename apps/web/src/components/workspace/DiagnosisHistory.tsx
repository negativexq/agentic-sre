import type { DiagnosisRevisionSummary } from "@/api/types";
import { useDiagnosisRevisions, useDiagnosisRevision } from "@/api/hooks";
import { useUrlFilters } from "@/lib/useUrlFilters";
import { Badge } from "@/components/ui/Badge";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { CopyButton } from "@/components/ui/CopyButton";
import { CodeBlock } from "@/components/ui/CodeBlock";
import { ErrorState, Skeleton } from "@/components/ui/States";
import { CoverageRecord } from "./EvidenceCoverage";
import { dateTime } from "@/lib/format";

function RevisionSummary({ revision }: { revision: DiagnosisRevisionSummary }) {
  return (
    <div className="min-w-0 space-y-3">
      <h3 className="text-sm font-medium">
        Revision {revision.revision_number}
      </h3>
      <p className="break-anywhere font-mono text-xs">
        {revision.root_cause ?? "No single actor presented in this revision"}
      </p>
      <div className="flex flex-wrap gap-2">
        <Badge>{revision.confidence}</Badge>
        <Badge>{revision.resolution ?? "Resolution not recorded"}</Badge>
      </div>
      <p className="text-xs text-subtle">
        {revision.trigger} · {dateTime(revision.created_at)}
      </p>
      <p className="break-anywhere font-mono text-[11px] text-muted">
        Run {revision.run_id ?? "not recorded"}
        {revision.run_id && (
          <CopyButton value={revision.run_id} label="revision run ID" />
        )}
      </p>
    </div>
  );
}

export function DiagnosisHistory({ incidentId }: { incidentId: string }) {
  const list = useDiagnosisRevisions(incidentId);
  const { params, update } = useUrlFilters();
  const revisions = list.data ?? [];
  const requested = params.get("revision");
  const number =
    requested === null
      ? (revisions.at(-1)?.revision_number ?? null)
      : Number(requested);
  const selected = revisions.find(
    (revision) => revision.revision_number === number,
  );
  const current = useDiagnosisRevision(
    incidentId,
    selected?.revision_number ?? null,
  );
  const previous = revisions.find(
    (revision) => revision.diagnosis_id === current.data?.previous_diagnosis_id,
  );
  const predecessor = useDiagnosisRevision(
    incidentId,
    previous?.revision_number ?? null,
  );
  if (list.isPending) return <Skeleton className="h-48" />;
  if (list.isError)
    return (
      <ErrorState
        message={`Could not load diagnosis revisions: ${(list.error as Error).message}`}
      />
    );
  if (!revisions.length)
    return (
      <p className="text-sm text-muted">
        No persisted diagnosis revisions recorded.
      </p>
    );
  return (
    <div className="grid items-start gap-4 lg:grid-cols-[230px_minmax(0,1fr)]">
      <Card>
        <CardHeader title="Stored revisions" />
        <CardBody className="space-y-2">
          {[...revisions].reverse().map((revision) => (
            <button
              key={revision.diagnosis_id}
              aria-pressed={revision === selected}
              onClick={() =>
                update({ revision: String(revision.revision_number) })
              }
              className={`block w-full rounded-md border p-3 text-left ${revision === selected ? "border-accent/40 bg-accent-soft" : "border-border hover:bg-surface"}`}
            >
              <span className="text-sm font-medium">
                Revision {revision.revision_number}
              </span>
              <span className="mt-1 block text-xs text-muted">
                {revision.trigger}
              </span>
              <time
                dateTime={revision.created_at}
                title={revision.created_at}
                className="mt-1 block text-[11px] text-subtle"
              >
                {dateTime(revision.created_at)}
              </time>
              <span className="mt-2 block text-[10px] text-muted">
                {revision.resolution ?? "Not recorded"}
              </span>
            </button>
          ))}
        </CardBody>
      </Card>
      <div className="min-w-0 space-y-4">
        {!selected ? (
          <ErrorState message="The requested revision is not recorded for this incident." />
        ) : current.isPending ? (
          <Skeleton className="h-48" />
        ) : current.isError ? (
          <ErrorState message={(current.error as Error).message} />
        ) : (
          current.data && (
            <>
              <Card>
                <CardHeader title="Persisted diagnosis comparison" />
                <CardBody className="space-y-4">
                  <p className="text-xs text-muted">
                    The difference is computed by the backend against this
                    revision's linked predecessor. No causal interpretation is
                    added here.
                  </p>
                  <div className="grid gap-5 sm:grid-cols-2">
                    {previous ? (
                      <RevisionSummary revision={previous} />
                    ) : (
                      <p className="text-sm text-muted">
                        {current.data.previous_diagnosis_id === null
                          ? "First revision · no predecessor recorded."
                          : "Predecessor metadata is not included in this revision list."}
                      </p>
                    )}
                    <RevisionSummary revision={current.data} />
                  </div>
                  {typeof current.data.diagnosis.summary === "string" && (
                    <p className="break-anywhere border-t border-border pt-3 text-sm text-muted">
                      {current.data.diagnosis.summary}
                    </p>
                  )}
                  <details>
                    <summary className="text-xs text-accent">
                      Revision identity, cutoffs & digests
                    </summary>
                    <dl className="mt-3 grid gap-3 sm:grid-cols-2">
                      {Object.entries({
                        "Diagnosis ID": current.data.diagnosis_id,
                        "Previous diagnosis ID":
                          current.data.previous_diagnosis_id,
                        "Observation cutoff": current.data.window_end,
                        "Engine version": current.data.engine_version,
                        "Config digest": current.data.config_digest,
                        "Manifest digest": current.data.manifest_digest,
                        "Tape digest": current.data.tape_digest,
                        "Epistemic digest": current.data.epistemic_digest,
                        Mode: current.data.mode,
                      }).map(([key, value]) => (
                        <div key={key}>
                          <dt className="text-xs text-subtle">{key}</dt>
                          <dd className="mt-1 break-anywhere font-mono text-xs">
                            {value ?? "Not recorded"}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </details>
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="Recorded predecessor difference" />
                <CardBody className="space-y-4">
                  {!current.data.diff ? (
                    <p className="text-sm text-muted">
                      No predecessor diff recorded for this revision.
                    </p>
                  ) : (
                    <>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge>
                          {current.data.diff.resolution_transition.previous}
                        </Badge>
                        <span>→</span>
                        <Badge>
                          {current.data.diff.resolution_transition.current}
                        </Badge>
                        <span className="text-xs text-subtle">
                          {current.data.diff.resolution_transition.changed
                            ? "Resolution changed"
                            : "Resolution unchanged"}
                        </span>
                      </div>
                      <p className="text-xs text-muted">
                        Manifest status:{" "}
                        {current.data.diff.manifest_diff.status}
                      </p>
                      <section>
                        <h3 className="mb-2 text-sm font-medium">
                          Hypothesis changes
                        </h3>
                        {current.data.diff.hypothesis_changes.length ? (
                          current.data.diff.hypothesis_changes.map(
                            (change, index) => (
                              <div
                                key={index}
                                className="mb-3 rounded border border-border p-3"
                              >
                                <p className="break-anywhere font-mono text-xs">
                                  {change.hypothesis_key}
                                </p>
                                <p className="mt-1 text-xs text-muted">
                                  {change.previous.state ??
                                    "State not recorded"}{" "}
                                  →{" "}
                                  {change.current.state ?? "State not recorded"}
                                </p>
                                <p className="mt-1 break-anywhere font-mono text-xs">
                                  {change.previous.causal_actor ??
                                    "Actor not recorded"}{" "}
                                  →{" "}
                                  {change.current.causal_actor ??
                                    "Actor not recorded"}
                                </p>
                                <p className="mt-1 text-xs text-subtle">
                                  Root eligible:{" "}
                                  {String(change.previous.root_eligible)} →{" "}
                                  {String(change.current.root_eligible)}
                                </p>
                              </div>
                            ),
                          )
                        ) : (
                          <p className="text-xs text-subtle">
                            No hypothesis state changes recorded.
                          </p>
                        )}
                      </section>
                      {(["appeared", "disappeared"] as const).map((kind) => (
                        <section key={kind}>
                          <h3 className="mb-2 text-sm font-medium">
                            {kind === "appeared"
                              ? "Appeared hypotheses"
                              : "Disappeared hypotheses"}
                          </h3>
                          {current.data!.diff![kind].length ? (
                            current.data!.diff![kind].map(
                              (hypothesis, index) => (
                                <p
                                  key={index}
                                  className="mb-2 break-anywhere font-mono text-xs text-muted"
                                >
                                  {hypothesis.hypothesis_id} ·{" "}
                                  {hypothesis.causal_actor ??
                                    "Actor not recorded"}{" "}
                                  · {hypothesis.state ?? "State not recorded"}
                                </p>
                              ),
                            )
                          ) : (
                            <p className="text-xs text-subtle">
                              None recorded.
                            </p>
                          )}
                        </section>
                      ))}
                      <section>
                        <h3 className="mb-2 text-sm font-medium">
                          New eliminations
                        </h3>
                        {current.data.diff.new_eliminations.length ? (
                          current.data.diff.new_eliminations.map(
                            (elimination, index) => (
                              <article
                                key={index}
                                className="mb-3 rounded border border-border p-3"
                              >
                                <p className="break-anywhere text-sm">
                                  {elimination.code} · {elimination.rule_id}
                                </p>
                                <p className="mt-1 break-anywhere text-xs text-muted">
                                  {elimination.mechanism}
                                </p>
                                <p className="mt-1 break-anywhere font-mono text-xs">
                                  Hypothesis {elimination.hypothesis_id}
                                </p>
                                <details className="mt-2">
                                  <summary className="text-xs text-accent">
                                    Elimination provenance & preconditions
                                  </summary>
                                  <CodeBlock
                                    value={JSON.stringify(elimination, null, 2)}
                                  />
                                </details>
                              </article>
                            ),
                          )
                        ) : (
                          <p className="text-xs text-subtle">None recorded.</p>
                        )}
                      </section>
                      <section>
                        <h3 className="mb-2 text-sm font-medium">
                          New decisive evidence IDs
                        </h3>
                        <p className="break-anywhere font-mono text-xs text-muted">
                          {current.data.diff.new_decisive_evidence_ids.join(
                            " · ",
                          ) || "None recorded."}
                        </p>
                      </section>
                      <details>
                        <summary className="text-xs text-accent">
                          Complete backend diff
                        </summary>
                        <CodeBlock
                          value={JSON.stringify(current.data.diff, null, 2)}
                        />
                      </details>
                    </>
                  )}
                </CardBody>
              </Card>
              <details className="rounded-lg border border-border p-4">
                <summary className="text-sm font-medium">
                  Coverage recorded with revision {current.data.revision_number}
                </summary>
                <div className="mt-4">
                  <CoverageRecord
                    coverage={current.data.diagnosis.evidence_coverage ?? null}
                  />
                </div>
              </details>
              <details className="rounded-lg border border-border p-4">
                <summary className="text-sm font-medium">
                  Stored diagnosis documents
                </summary>
                <div className="mt-3 space-y-4">
                  {previous &&
                    (predecessor.isPending ? (
                      <Skeleton className="h-24" />
                    ) : predecessor.isError ? (
                      <ErrorState
                        message={(predecessor.error as Error).message}
                      />
                    ) : (
                      <details>
                        <summary className="text-xs text-accent">
                          Predecessor revision {previous.revision_number}
                        </summary>
                        <CodeBlock
                          value={JSON.stringify(
                            predecessor.data?.diagnosis,
                            null,
                            2,
                          )}
                        />
                      </details>
                    ))}
                  <details>
                    <summary className="text-xs text-accent">
                      Selected revision {current.data.revision_number}
                    </summary>
                    <CodeBlock
                      value={JSON.stringify(current.data.diagnosis, null, 2)}
                    />
                  </details>
                </div>
              </details>
            </>
          )
        )}
      </div>
    </div>
  );
}
