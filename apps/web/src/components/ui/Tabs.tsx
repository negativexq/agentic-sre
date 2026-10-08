// shadcn/ui Tabs composition on Radix, sharing the console's semantic tokens.
import { type ReactNode, useState } from "react";
import * as Primitive from "@radix-ui/react-tabs";
export type TabDef = { id: string; label: ReactNode; content: ReactNode };
export function Tabs({
  tabs,
  initial,
  value,
  onChange,
}: {
  tabs: TabDef[];
  initial?: string;
  value?: string;
  onChange?: (value: string) => void;
}) {
  const [active, setActive] = useState(initial ?? tabs[0]?.id);
  return (
    <Primitive.Root
      value={value ?? active}
      onValueChange={onChange ?? setActive}
    >
      <Primitive.List
        aria-label="Investigation views"
        className="flex flex-wrap gap-1 border-b border-border"
      >
        {tabs.map((tab) => (
          <Primitive.Trigger
            key={tab.id}
            value={tab.id}
            className="-mb-px inline-flex items-center gap-2 border-b-2 border-transparent px-3 py-3 text-xs font-medium text-muted transition-colors hover:text-text data-[state=active]:border-accent data-[state=active]:text-accent"
          >
            {tab.label}
          </Primitive.Trigger>
        ))}
      </Primitive.List>
      {tabs.map((tab) => (
        <Primitive.Content
          key={tab.id}
          value={tab.id}
          className="pt-5 outline-none focus-visible:outline-accent"
        >
          {tab.content}
        </Primitive.Content>
      ))}
    </Primitive.Root>
  );
}
