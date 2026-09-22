import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/States";

export function ReportsPage() {
  return (
    <>
      <PageHeader title="Reports" />
      <EmptyState title="The reports library lands in M9." />
    </>
  );
}
