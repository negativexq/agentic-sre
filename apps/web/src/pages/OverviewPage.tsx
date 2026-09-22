import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/States";

export function OverviewPage() {
  return (
    <>
      <PageHeader title="Overview" description="What is happening across the cluster right now." />
      <EmptyState title="Dashboard wiring lands in M3." description="Active incidents, counters and system health render here." />
    </>
  );
}
