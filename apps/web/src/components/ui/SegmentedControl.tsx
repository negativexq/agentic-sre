import * as Tabs from "@radix-ui/react-tabs";

export function SegmentedControl({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string; count: number }[];
}) {
  return (
    <Tabs.Root value={value} onValueChange={onChange}>
      <Tabs.List
        aria-label={label}
        className="flex flex-wrap gap-1 rounded-lg border border-border bg-surface p-1"
      >
        {options.map((option) => (
          <Tabs.Trigger
            key={option.value}
            value={option.value}
            className="inline-flex min-h-9 items-center gap-2 rounded-md px-3 text-xs font-medium text-muted hover:text-text data-[state=active]:bg-surface-raised data-[state=active]:text-accent"
          >
            {option.label}
            <span className="rounded bg-unverified-soft px-1.5 font-mono text-[10px] text-subtle">
              {option.count}
            </span>
          </Tabs.Trigger>
        ))}
      </Tabs.List>
    </Tabs.Root>
  );
}
