import type { HypothesisView } from "@/api/types";
import { Drawer } from "@/components/ui/Drawer";
import { Badge } from "@/components/ui/Badge";
import { CopyButton } from "@/components/ui/CopyButton";
import { Entity } from "@/components/Entity";

export function HypothesisInspector({
  id,
  hypotheses,
  onClose,
}: {
  id: string | null;
  hypotheses: HypothesisView[];
  onClose: () => void;
}) {
  const hypothesis = hypotheses.find((item) => item.hypothesis_id === id);
  return (
    <Drawer open={id !== null} onClose={onClose} title="Hypothesis inspector">
      {hypothesis ? (
        <div className="space-y-5">
          <Entity value={hypothesis.actor} />
          <Badge
            tone={
              hypothesis.epistemic_state === "SUPPORTED" ? "accent" : "neutral"
            }
          >
            {hypothesis.epistemic_state}
          </Badge>
          <p className="text-xs text-subtle">
            Support does not establish this candidate as the root cause. The
            engine's explanation remains authoritative.
          </p>
          <section>
            <h3 className="mb-2 text-sm font-medium">Engine explanation</h3>
            <p className="break-anywhere text-sm text-muted">
              {hypothesis.causal_explanation || "No explanation recorded."}
            </p>
          </section>
          <section>
            <h3 className="mb-2 text-sm font-medium">Engine reasons</h3>
            {hypothesis.reasons.length ? (
              <ul className="list-inside list-disc space-y-2 text-sm text-muted">
                {hypothesis.reasons.map((reason, index) => (
                  <li key={index} className="break-anywhere">
                    {reason}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted">No reasons recorded.</p>
            )}
          </section>
          <dl className="space-y-3 text-xs">
            <div>
              <dt className="text-subtle">Hypothesis ID</dt>
              <dd className="break-anywhere font-mono">
                {hypothesis.hypothesis_id}
                <CopyButton
                  value={hypothesis.hypothesis_id}
                  label="hypothesis ID"
                />
              </dd>
            </div>
            <div>
              <dt className="text-subtle">Engine score</dt>
              <dd>{hypothesis.score} · not a probability</dd>
            </div>
          </dl>
        </div>
      ) : (
        <p className="text-sm text-muted">
          No hypothesis with this exact ID is included in the current diagnosis.
          No explanation or actor is inferred from the reference.
        </p>
      )}
    </Drawer>
  );
}
