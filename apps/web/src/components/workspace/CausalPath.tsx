import { ArrowRight } from "lucide-react";
import type { CausalHopView } from "@/api/types";
import { Entity } from "@/components/Entity";
import { useInvestigationCanvas } from "@/components/investigation/context";
import { CausalXRay } from "@/components/investigation/CausalXRay";

/** Each row is exactly one engine hop. Disjoint hops are never stitched together. */
export function CausalPath({
  hops,
  onOpenResource,
}: {
  hops: CausalHopView[];
  onOpenResource?: (resource: string) => void;
}) {
  const canvas = useInvestigationCanvas();
  if (!hops.length)
    return (
      <p className="text-sm text-muted">
        No causal path recorded by the engine.
      </p>
    );
  return (
    <div className="space-y-3">
      {!canvas?.expanded && (
        <CausalXRay
          hops={hops}
          onSelectResource={(resource) => onOpenResource?.(resource)}
        />
      )}
      {canvas?.expanded && (
        <p className="text-xs text-subtle">
          Causal X-Ray follows the shared selection in the investigation canvas
          above.
        </p>
      )}
      <details>
        <summary className="text-xs text-accent">Recorded hop ledger</summary>
        <ol className="space-y-3">
          {hops.map((hop, index) => (
            <li
              key={index}
              className="grid min-w-0 grid-cols-[1fr] items-center gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(90px,auto)_minmax(0,1fr)]"
            >
              <div
                data-canvas-highlight={
                  canvas?.focus.resources.has(hop.source) || undefined
                }
                className={`rounded-md border p-3 ${canvas?.focus.resources.has(hop.source) ? "border-accent bg-accent-soft" : "border-border bg-surface"}`}
              >
                {onOpenResource ? (
                  <button
                    onClick={() => onOpenResource(hop.source)}
                    aria-label={`Inspect resource ${hop.source}`}
                    className="w-full text-left hover:text-accent"
                  >
                    <Entity value={hop.source} />
                  </button>
                ) : (
                  <Entity value={hop.source} />
                )}
              </div>
              <div className="flex flex-col items-center gap-1 py-1 text-subtle">
                <span className="break-anywhere text-center font-mono text-[11px]">
                  {hop.relation}
                </span>
                <ArrowRight size={16} aria-hidden />
                <span className="text-[10px]">{hop.direction}</span>
              </div>
              <div
                data-canvas-highlight={
                  canvas?.focus.resources.has(hop.target) || undefined
                }
                className={`rounded-md border p-3 ${canvas?.focus.resources.has(hop.target) ? "border-accent bg-accent-soft" : "border-border bg-surface"}`}
              >
                {onOpenResource ? (
                  <button
                    onClick={() => onOpenResource(hop.target)}
                    aria-label={`Inspect resource ${hop.target}`}
                    className="w-full text-left hover:text-accent"
                  >
                    <Entity value={hop.target} />
                  </button>
                ) : (
                  <Entity value={hop.target} />
                )}
              </div>
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}
