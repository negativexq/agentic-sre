import { Link } from "react-router-dom";

import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/States";
import { Button } from "@/components/ui/Button";

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Not found" />
      <EmptyState
        title="That page does not exist."
        action={
          <Link to="/">
            <Button variant="primary">Back to overview</Button>
          </Link>
        }
      />
    </>
  );
}
