import { useState } from "react";

import { useReportDeliveries, useShareReport, useSystemStatus } from "@/api/hooks";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/States";
import { dateTime } from "@/lib/format";

export function ShareReport({ reportId }: { reportId: string }) {
  const { data: system } = useSystemStatus();
  const { data: deliveries } = useReportDeliveries(reportId);
  const share = useShareReport(reportId);
  const [recipients, setRecipients] = useState("");
  const [includePdf, setIncludePdf] = useState(true);

  const emailConnector = system?.connectors.find((connector) => connector.name === "Email");
  const configured = emailConnector?.status === "connected";

  const submit = () => {
    const list = recipients
      .split(/[,\s]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (list.length === 0) return;
    share.mutate({ recipients: list, include_pdf: includePdf });
  };

  return (
    <div className="mt-3 border-t border-border pt-3">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-subtle">
        Share by email
      </p>

      {!configured && (
        <Alert tone="warning">
          Email delivery is not configured. Set <code>SRE_SMTP_HOST</code> to enable sharing. You can
          still export the PDF, Markdown or JSON above.
        </Alert>
      )}

      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="flex flex-1 flex-col gap-1 text-xs text-subtle">
          Recipients
          <input
            value={recipients}
            onChange={(event) => setRecipients(event.target.value)}
            placeholder="sre@example.com, oncall@example.com"
            disabled={!configured}
            className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-text focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"
          />
        </label>
        <label className="flex items-center gap-2 pb-1.5 text-sm text-muted">
          <input
            type="checkbox"
            checked={includePdf}
            onChange={(event) => setIncludePdf(event.target.checked)}
            disabled={!configured}
          />
          Attach PDF
        </label>
        <Button
          variant="primary"
          onClick={submit}
          disabled={!configured || share.isPending || recipients.trim() === ""}
        >
          {share.isPending ? "Sending…" : "Send"}
        </Button>
      </div>

      {share.isError && (
        <p className="mt-2 text-sm text-critical">{(share.error as Error).message}</p>
      )}
      {share.isSuccess && <p className="mt-2 text-sm text-healthy">Report sent.</p>}

      {deliveries && deliveries.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-muted">
          {deliveries.map((delivery) => (
            <li key={delivery.delivery_id} className="flex flex-wrap justify-between gap-2">
              <span className="break-anywhere">
                {delivery.status === "sent" ? "✓" : "✗"} {delivery.recipients.join(", ")}
              </span>
              <span className="text-subtle">{dateTime(delivery.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
