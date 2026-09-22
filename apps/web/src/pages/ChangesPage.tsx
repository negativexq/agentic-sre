import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/States";

export function ChangesPage() {
  return (
    <>
      <PageHeader title="Changes" />
      <EmptyState title="The change explorer lands in M6." />
    </>
  );
}
