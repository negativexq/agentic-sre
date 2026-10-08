import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

export function CopyButton({ value, label }: { value: string; label: string }) {
  const [result, setResult] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <span className="inline-flex shrink-0 items-center">
      <button
        type="button"
        aria-label={`Copy ${label}`}
        title={`Copy ${label}`}
        className="rounded-md p-1.5 text-subtle hover:bg-surface hover:text-accent"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setResult("copied");
          } catch {
            setResult("failed");
          }
          clearTimeout(timer.current);
          timer.current = setTimeout(() => setResult("idle"), 2500);
        }}
      >
        {result === "copied" ? (
          <Check size={14} aria-hidden />
        ) : (
          <Copy size={14} aria-hidden />
        )}
      </button>
      <span
        role="status"
        className={result === "failed" ? "text-xs text-critical" : "sr-only"}
      >
        {result === "copied"
          ? `${label} copied`
          : result === "failed"
            ? "Copy unavailable; select the text to copy."
            : ""}
      </span>
    </span>
  );
}
