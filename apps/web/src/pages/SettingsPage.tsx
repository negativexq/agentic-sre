import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/States";

export function SettingsPage() {
  return (
    <>
      <PageHeader title="Settings" />
      <EmptyState title="Read-only settings land in M11." />
    </>
  );
}
