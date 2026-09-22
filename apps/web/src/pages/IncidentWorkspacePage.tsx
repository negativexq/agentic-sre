import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/States";

export function IncidentWorkspacePage() {
  return (
    <>
      <PageHeader title="Incident" />
      <EmptyState title="The incident workspace lands in M4." />
    </>
  );
}
