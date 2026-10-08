import { ArrowRight } from "lucide-react";
import type { CausalHopView } from "@/api/types";
import { Entity } from "@/components/Entity";

/** Each row is exactly one engine hop. Disjoint hops are never stitched together. */
export function CausalPath({
  hops,
  onOpenResource,
}: {
  hops: CausalHopView[];
  onOpenResource?: (resource: string) => void;
}) {
  if (!hops.length)
    return (
      <p className="text-sm text-muted">
        No causal path recorded by the engine.
      </p>
    );
  return (
    <ol className="space-y-3">
      {hops.map((hop, index) => (
        <li
          key={index}
          className="grid min-w-0 grid-cols-[1fr] items-center gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(90px,auto)_minmax(0,1fr)]"
        >
          <div className="rounded-md border border-border bg-surface p-3">
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
          <div className="rounded-md border border-border bg-surface p-3">
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
  );
}
