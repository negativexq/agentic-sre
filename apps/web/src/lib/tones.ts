import type { BadgeTone } from "@/components/ui/Badge";

/** Map an engine confidence label to a badge tone. */
export function confidenceTone(confidence: string | null | undefined): BadgeTone {
  switch (confidence) {
    case "VERIFIED":
      return "verified";
    case "LIKELY":
      return "likely";
    default:
      return "unverified";
  }
}

/**
 * Resolution tone. AMBIGUOUS and INSUFFICIENT_EVIDENCE are intentionally
 * neutral, never critical — an unresolved diagnosis is honest, not wrong.
 */
export function resolutionTone(resolution: string | null | undefined): BadgeTone {
  switch (resolution) {
    case "RESOLVED":
      return "healthy";
    case "AMBIGUOUS":
      return "accent";
    default:
      return "neutral";
  }
}

/** Severity tone maps to impact color, a separate axis from resolution. */
export function severityTone(severity: string | null | undefined): BadgeTone {
  switch (severity) {
    case "CRITICAL":
      return "critical";
    case "WARNING":
      return "warning";
    default:
      return "neutral";
  }
}
