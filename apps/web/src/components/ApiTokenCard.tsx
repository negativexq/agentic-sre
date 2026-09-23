import { useState } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { clearToken, getToken, setToken } from "@/lib/authToken";

/**
 * Client-only entry for the API bearer token. Needed only when the server has
 * `SRE_API_TOKEN` set — report generation and email sharing then require it. The
 * token is held in sessionStorage (this tab only), attached to write requests by
 * the API client, and never persisted to the bundle, env, or localStorage.
 */
export function ApiTokenCard({ required }: { required: boolean }) {
  const [value, setValue] = useState("");
  const [held, setHeld] = useState(() => Boolean(getToken()));

  const save = () => {
    setToken(value.trim());
    setHeld(Boolean(value.trim()));
    setValue("");
  };

  const clear = () => {
    clearToken();
    setHeld(false);
  };

  return (
    <Card>
      <CardHeader
        title="Console access"
        action={
          held ? (
            <Badge tone="healthy">Token set (this session)</Badge>
          ) : (
            <Badge tone={required ? "warning" : "neutral"}>No token</Badge>
          )
        }
      />
      <CardBody className="space-y-3">
        <p className="text-sm text-muted">
          {required
            ? "This server requires an API token for report generation and email sharing. Paste it here to enable those actions."
            : "The server has no API token configured, so writes are open. Set a token here only if the server requires one."}
        </p>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-1 flex-col gap-1 text-xs text-subtle">
            API token
            <input
              type="password"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder="paste SRE_API_TOKEN"
              className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-text focus-visible:outline-2 focus-visible:outline-accent"
            />
          </label>
          <Button variant="primary" onClick={save} disabled={value.trim() === ""}>
            Save
          </Button>
          <Button variant="secondary" onClick={clear} disabled={!held}>
            Clear
          </Button>
        </div>
        <p className="text-xs text-subtle">
          Held in this browser tab only (sessionStorage); cleared when the tab closes.
        </p>
      </CardBody>
    </Card>
  );
}
