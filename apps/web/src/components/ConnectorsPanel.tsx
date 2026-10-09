// The Connections page's Connector registry and the "Connect a cluster" flow (docs/ui/connect-cluster-design.md).
import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Plus } from "lucide-react";

import {
  useConnectorPreflight,
  useConnectors,
  useCreateConnector,
  useDisableConnector,
} from "@/api/hooks";
import type {
  ConnectorView,
  ConnectorsView,
  CreatedConnector,
  PreflightCheck,
} from "@/api/types";
import { Badge, type BadgeTone } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { CopyButton } from "@/components/ui/CopyButton";
import { Drawer } from "@/components/ui/Drawer";
import { Input } from "@/components/ui/Input";
import { Alert, EmptyState } from "@/components/ui/States";
import { Table, TCell, THead, TRow } from "@/components/ui/Table";
import { dateTime } from "@/lib/format";

// The API's rule: a Connector id is a DNS label.
const ID = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

const STATUS_TONE: Record<string, BadgeTone> = {
  active: "healthy",
  pending: "warning",
  disabled: "neutral",
};

const CHECK_TONE: Record<string, BadgeTone> = {
  ok: "healthy",
  warning: "warning",
  failed: "critical",
  not_configured: "neutral",
};

const CHECK_LABEL: Record<string, string> = {
  ok: "OK",
  warning: "Warning",
  failed: "Failed",
  not_configured: "Not configured",
};

export function PreflightTable({ checks }: { checks: PreflightCheck[] }) {
  return (
    <Table>
      <THead columns={["Check", "Result", "Detail"]} />
      <tbody>
        {checks.map((check) => (
          <TRow key={check.name}>
            <TCell className="font-mono text-xs">{check.name}</TCell>
            <TCell>
              <Badge tone={CHECK_TONE[check.status] ?? "neutral"}>
                {CHECK_LABEL[check.status] ?? check.status}
              </Badge>
            </TCell>
            <TCell className="break-anywhere text-xs text-muted">
              {check.detail || "—"}
            </TCell>
          </TRow>
        ))}
      </tbody>
    </Table>
  );
}

function DisableDialog({
  connector,
  onClose,
}: {
  connector: string | null;
  onClose: () => void;
}) {
  const disable = useDisableConnector();
  return (
    <Dialog.Root
      open={connector !== null}
      onOpenChange={(open) => {
        if (!open) {
          disable.reset();
          onClose();
        }
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(4_9_16_/_60%)]" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100vw-32px)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border-strong bg-surface-raised p-5 text-text shadow-md">
          <Dialog.Title className="text-sm font-semibold">
            Disable {connector}?
          </Dialog.Title>
          <Dialog.Description className="mt-2 text-sm text-muted">
            Its certificate is revoked and its live session closes within
            seconds. This cannot be undone from the console; to connect the
            cluster again, create a new Connector.
          </Dialog.Description>
          {disable.isError && (
            <div className="mt-3">
              <Alert tone="critical">{(disable.error as Error).message}</Alert>
            </div>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <Dialog.Close asChild>
              <Button variant="ghost">Cancel</Button>
            </Dialog.Close>
            <Button
              variant="primary"
              className="border-critical bg-critical"
              disabled={disable.isPending}
              onClick={() =>
                connector &&
                disable.mutate(connector, { onSuccess: () => onClose() })
              }
            >
              Disable
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function ConnectorRow({
  connector,
  writable,
  onDisable,
}: {
  connector: ConnectorView;
  writable: boolean;
  onDisable: (id: string) => void;
}) {
  const preflight = useConnectorPreflight();
  return (
    <>
      <TRow>
        <TCell className="font-mono text-xs">{connector.id}</TCell>
        <TCell>
          <Badge tone={STATUS_TONE[connector.status] ?? "neutral"}>
            {connector.status}
          </Badge>
        </TCell>
        <TCell className="text-xs">
          <span className="inline-flex items-center gap-2">
            <span
              className={
                connector.connected
                  ? "h-1.5 w-1.5 rounded-full bg-healthy"
                  : "h-1.5 w-1.5 rounded-full bg-border-strong"
              }
            />
            {connector.connected ? "Connected" : "Not connected"}
          </span>
        </TCell>
        <TCell className="text-xs text-muted">
          {dateTime(connector.certificate_valid_until)}
          {connector.renews_after && (
            <span className="block text-[11px] text-subtle">
              renews around {dateTime(connector.renews_after)}
            </span>
          )}
        </TCell>
        <TCell className="text-xs text-muted">
          {dateTime(connector.enrolled_at)}
        </TCell>
        <TCell className="text-right">
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              className="text-xs"
              disabled={!connector.connected || preflight.isPending}
              onClick={() => preflight.mutate(connector.id)}
            >
              {preflight.isPending ? "Running…" : "Run preflight"}
            </Button>
            {writable && connector.status !== "disabled" && (
              <Button
                variant="ghost"
                className="text-xs text-critical"
                onClick={() => onDisable(connector.id)}
              >
                Disable
              </Button>
            )}
          </div>
        </TCell>
      </TRow>
      {(preflight.data || preflight.isError) && (
        <tr>
          <td colSpan={6} className="px-3 pb-4">
            {preflight.isError ? (
              <Alert tone="critical">
                {(preflight.error as Error).message}
              </Alert>
            ) : (
              <PreflightTable checks={preflight.data ?? []} />
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export function ConnectorsTable({ view }: { view: ConnectorsView }) {
  const [disabling, setDisabling] = useState<string | null>(null);
  if (view.connectors.length === 0)
    return (
      <EmptyState
        title="No Connector yet"
        description="Connect a cluster to let the control plane read its changes, alerts and evidence."
      />
    );
  return (
    <>
      <Table>
        <THead
          columns={[
            "Connector",
            "Status",
            "Session",
            "Certificate valid until",
            "Enrolled",
            "",
          ]}
        />
        <tbody>
          {view.connectors.map((connector) => (
            <ConnectorRow
              key={connector.id}
              connector={connector}
              writable={view.writable}
              onDisable={setDisabling}
            />
          ))}
        </tbody>
      </Table>
      <DisableDialog connector={disabling} onClose={() => setDisabling(null)} />
    </>
  );
}

function NameStep({ onCreated }: { onCreated: (c: CreatedConnector) => void }) {
  const [id, setId] = useState("");
  const create = useCreateConnector();
  const valid = ID.test(id);
  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) create.mutate(id, { onSuccess: onCreated });
      }}
    >
      <div className="space-y-1.5">
        <label htmlFor="connector-id" className="text-sm font-medium">
          Connector name
        </label>
        <Input
          id="connector-id"
          value={id}
          autoComplete="off"
          spellCheck={false}
          placeholder="prod-eu-1"
          aria-invalid={id !== "" && !valid}
          onChange={(event) => setId(event.target.value.trim())}
        />
        <p className="text-xs text-subtle">
          Lowercase letters, digits and “-”; it also names the Helm release.
        </p>
        {id !== "" && !valid && (
          <p className="text-xs text-critical">
            Not a DNS label: use a-z, 0-9 and “-”, starting and ending with a
            letter or digit.
          </p>
        )}
      </div>
      {create.isError && (
        <Alert tone="critical">{(create.error as Error).message}</Alert>
      )}
      <Button
        type="submit"
        variant="primary"
        disabled={!valid || create.isPending}
      >
        {create.isPending ? "Creating…" : "Create Connector"}
      </Button>
    </form>
  );
}

function InstallStep({
  created,
  onNext,
}: {
  created: CreatedConnector;
  onNext: () => void;
}) {
  return (
    <div className="space-y-4">
      <Alert tone="warning">
        The token is valid until {dateTime(created.expires_at)} and works once.
        It will not be shown again.
      </Alert>
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium">One-time token</span>
          <CopyButton value={created.token} label="token" />
        </div>
        <pre
          data-testid="connector-token"
          className="overflow-x-auto rounded-lg border border-border bg-surface p-3 font-mono text-xs"
        >
          {created.token}
        </pre>
      </div>
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium">Install with Helm</span>
          <CopyButton value={created.install_command} label="install command" />
        </div>
        <pre
          data-testid="install-command"
          className="overflow-x-auto rounded-lg border border-border bg-surface p-3 font-mono text-xs leading-relaxed"
        >
          {created.install_command}
        </pre>
        <p className="text-xs text-subtle">
          Replace the <code>&lt;…&gt;</code> placeholders: the namespaces to
          watch and the URLs of your Alertmanager, Prometheus, Loki and Tempo as
          the cluster reaches them. Run it from a checkout of this repository.
        </p>
        {!created.endpoints_configured && (
          <Alert tone="info">
            The control plane does not know the address your cluster reaches it
            at. Fill in <code>&lt;control-plane-host&gt;</code>, or set{" "}
            <code>SRE_CONNECTOR_PUBLIC_ENDPOINT</code> and{" "}
            <code>SRE_CONNECTOR_PUBLIC_ENROLL_ENDPOINT</code> on the control
            plane.
          </Alert>
        )}
      </div>
      <Button variant="primary" onClick={onNext}>
        I have run it
      </Button>
    </div>
  );
}

function WaitingStep({ id }: { id: string }) {
  const { data } = useConnectors(true);
  const preflight = useConnectorPreflight();
  const connected = Boolean(
    data?.connectors.find((connector) => connector.id === id)?.connected,
  );
  const started = useRef(false);
  const { mutate } = preflight;
  useEffect(() => {
    if (connected && !started.current) {
      started.current = true;
      mutate(id);
    }
  }, [connected, id, mutate]);
  if (!connected)
    return (
      <div role="status" className="space-y-2">
        <p className="text-sm">
          Waiting for <span className="font-mono">{id}</span> to connect…
        </p>
        <p className="text-xs text-subtle">
          The Connector enrolls with its token, then opens its session. This
          usually takes under a minute after the pod starts.
        </p>
      </div>
    );
  return (
    <div className="space-y-4">
      <p role="status" className="text-sm">
        <span className="font-mono">{id}</span> is connected.{" "}
        {preflight.isPending && "Running preflight…"}
      </p>
      {preflight.isError && (
        <Alert tone="critical">{(preflight.error as Error).message}</Alert>
      )}
      {preflight.data && (
        <>
          <PreflightTable checks={preflight.data} />
          <p className="text-xs text-subtle">
            Failures and warnings name what is missing or broader than
            read-only; fix them in the chart's values and run preflight again
            from the Connectors table.
          </p>
        </>
      )}
    </div>
  );
}

export function ConnectDrawer({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [created, setCreated] = useState<CreatedConnector | null>(null);
  const [waiting, setWaiting] = useState(false);
  const step = !created ? 1 : waiting ? 3 : 2;
  return (
    <Drawer
      open={open}
      onClose={onClose}
      onAfterClose={() => {
        // The token is shown once: closing the drawer forgets it.
        setCreated(null);
        setWaiting(false);
      }}
      title="Connect a cluster"
    >
      <ol className="mb-5 flex gap-4 text-xs text-subtle" aria-label="Steps">
        {["Name", "Install", "Connect"].map((label, index) => (
          <li
            key={label}
            aria-current={step === index + 1 ? "step" : undefined}
            className={step === index + 1 ? "font-medium text-text" : ""}
          >
            {index + 1}. {label}
          </li>
        ))}
      </ol>
      {step === 1 && <NameStep onCreated={setCreated} />}
      {step === 2 && created && (
        <InstallStep created={created} onNext={() => setWaiting(true)} />
      )}
      {step === 3 && created && <WaitingStep id={created.id} />}
    </Drawer>
  );
}

export function ConnectButton({ disabled }: { disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        variant="primary"
        className="text-xs"
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <Plus size={14} aria-hidden />
        Connect a cluster
      </Button>
      <ConnectDrawer open={open} onClose={() => setOpen(false)} />
    </>
  );
}
