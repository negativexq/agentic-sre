import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/States";

export function ConnectionsPage() {
  return (
    <>
      <PageHeader title="Connections" />
      <EmptyState title="Connector health lands in M11." />
    </>
  );
}
