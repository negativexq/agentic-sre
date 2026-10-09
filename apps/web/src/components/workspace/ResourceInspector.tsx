import { useRef } from "react";
import type { DiagnosisView, FindingView } from "@/api/types";
import { Drawer } from "@/components/ui/Drawer";
import { Entity } from "@/components/Entity";
import { CopyButton } from "@/components/ui/CopyButton";
import { FindingsList } from "./FindingsList";

export function ResourceInspector({
  resource,
  diagnosis,
  onClose,
  onSelect,
}: {
  resource: string | null;
  diagnosis: DiagnosisView;
  onClose: () => void;
  onSelect: (finding: FindingView) => void;
}) {
  const pending = useRef<FindingView | null>(null);
  const findings = diagnosis.evidence.filter(
    (finding) => finding.entity === resource,
  );
  const hops = diagnosis.causal_path.filter(
    (hop) => hop.source === resource || hop.target === resource,
  );
  const witnesses = (diagnosis.executing_instances ?? []).filter((witness) =>
    [witness.actor, witness.instance, witness.target].includes(resource),
  );
  return (
    <Drawer
      open={resource !== null}
      onClose={onClose}
      title="Resource inspector"
      restoreFocusLabel={resource ? `Select graph resource ${resource}` : null}
      onAfterClose={() => {
        if (pending.current) {
          onSelect(pending.current);
          pending.current = null;
        }
      }}
    >
      {resource && (
        <div className="space-y-5">
          <div>
            <Entity value={resource} />
            <CopyButton value={resource} label="resource identity" />
          </div>
          <p className="text-xs text-muted">
            Incident context only, matched by exact recorded identity. No live
            Kubernetes status or topology is inferred.
          </p>
          <section>
            <h3 className="mb-2 text-sm font-medium">
              Recorded findings ({findings.length})
            </h3>
            <FindingsList
              findings={findings}
              onSelect={(finding) => {
                pending.current = finding;
                onClose();
              }}
            />
          </section>
          <section>
            <h3 className="mb-2 text-sm font-medium">
              Engine path relationships
            </h3>
            {hops.length ? (
              hops.map((hop, index) => (
                <p
                  key={index}
                  className="mb-3 break-anywhere font-mono text-xs text-muted"
                >
                  {hop.source} → {hop.relation} ({hop.direction}) → {hop.target}
                </p>
              ))
            ) : (
              <p className="text-xs text-muted">
                No path relationships recorded for this exact identity.
              </p>
            )}
          </section>
          <section>
            <h3 className="mb-2 text-sm font-medium">Execution witnesses</h3>
            {witnesses.length ? (
              witnesses.map((witness, index) => (
                <dl
                  key={index}
                  className="mb-3 space-y-2 break-anywhere font-mono text-xs text-muted"
                >
                  {Object.entries(witness).map(([key, value]) => (
                    <div key={key}>
                      <dt className="text-subtle">{key}</dt>
                      <dd>{value ?? "Not recorded"}</dd>
                    </div>
                  ))}
                </dl>
              ))
            ) : (
              <p className="text-xs text-muted">
                No execution witness references this exact identity.
              </p>
            )}
          </section>
        </div>
      )}
    </Drawer>
  );
}
