import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/States";

export function IncidentsPage() {
  return (
    <>
      <PageHeader title="Incidents" />
      <EmptyState title="Filterable incident list lands in M3." />
    </>
  );
}
