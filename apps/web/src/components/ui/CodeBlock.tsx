import { useState } from "react";

import { Button } from "@/components/ui/Button";

/** Monospace JSON/text block with copy, used for raw evidence drill-down. */
export function CodeBlock({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  const copy = () => {
    void navigator.clipboard?.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  };

  return (
    <div className="relative">
      <Button
        variant="ghost"
        className="absolute right-2 top-2 h-7 px-2 py-0 text-xs"
        onClick={copy}
      >
        {copied ? "Copied" : "Copy"}
      </Button>
      <pre className="overflow-x-auto rounded-lg border border-border bg-surface p-3 text-xs leading-relaxed">
        <code className="font-mono text-text">{value}</code>
      </pre>
    </div>
  );
}
