import type { DiagnosisView } from "@/api/types";
import { TCell, THead, TRow, Table } from "@/components/ui/Table";
import { clock } from "@/lib/format";

export function TimingSection({ diagnosis }: { diagnosis: DiagnosisView }) {
  const timing = diagnosis.timing;
  if (!timing)
    return (
      <p className="text-sm text-subtle">
        No timing assessment recorded for this diagnosis.
      </p>
    );
  return (
    <section className="space-y-2">
      <div className="text-xs font-medium uppercase tracking-wide text-subtle">
        Timing
      </div>
      {timing.status === "UNASSESSED" ? (
        <p className="text-sm text-subtle">
          Timing stability not assessed for this source.
        </p>
      ) : (
        <p className="text-sm text-muted">
          {timing.status === "STABLE"
            ? "The decision holds over every admissible onset."
            : "The decision depends on the exact onset."}
        </p>
      )}
      {timing.withheld.length > 0 && (
        <Table>
          <THead columns={["Withheld authority", "Actor", "Relations"]} />
          <tbody>
            {timing.withheld.map((item, index) => (
              <TRow key={index}>
                <TCell>{item.authority}</TCell>
                <TCell className="break-anywhere">{item.actor}</TCell>
                <TCell className="text-muted">
                  {item.relations.join(", ") || "—"}
                </TCell>
              </TRow>
            ))}
          </tbody>
        </Table>
      )}
      {timing.drivers.length > 0 && (
        <Table>
          <THead
            columns={["Onset", "Status there", "Actor", "Change", "Reason"]}
          />
          <tbody>
            {timing.drivers.map((item, index) => (
              <TRow key={index}>
                <TCell className="whitespace-nowrap">{clock(item.onset)}</TCell>
                <TCell>{item.diagnosis_status}</TCell>
                <TCell className="break-anywhere">{item.actor}</TCell>
                <TCell>{item.change}</TCell>
                <TCell className="text-muted">{item.reason}</TCell>
              </TRow>
            ))}
          </tbody>
        </Table>
      )}
    </section>
  );
}
