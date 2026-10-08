// shadcn/ui Select composition adapted to the console's semantic tokens.
import * as Select from "@radix-ui/react-select";
import { Check, ChevronDown, ChevronUp, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

const ANY = "__any__";
function filterLabel(value: string) {
  return value
    .toLowerCase()
    .replaceAll("_", " ")
    .replace(/^./, (c) => c.toUpperCase());
}

export function FilterSelect({
  label,
  value,
  options,
  onChange,
  icon: Icon,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
  icon: LucideIcon;
}) {
  return (
    <Select.Root
      value={value || ANY}
      onValueChange={(v) => onChange(v === ANY ? "" : v)}
    >
      <Select.Trigger
        aria-label={label}
        title={`${label}: ${value ? filterLabel(value) : "Any"}`}
        className={cn(
          "flex h-10 min-w-0 w-full items-center gap-2 rounded-md border px-3 text-sm transition-colors hover:border-border-strong focus-visible:outline-2 focus-visible:outline-accent",
          value
            ? "border-accent/40 bg-accent-soft text-accent"
            : "border-border-strong bg-surface-raised text-muted",
        )}
      >
        <Icon size={15} aria-hidden className="shrink-0" />
        <span className="shrink-0 text-xs">{label}</span>
        <span aria-hidden className="h-4 border-l border-current opacity-25" />
        <span className="min-w-0 flex-1 truncate text-left font-medium">
          <Select.Value />
        </span>
        <Select.Icon>
          <ChevronDown size={14} aria-hidden />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content
          position="popper"
          sideOffset={6}
          collisionPadding={12}
          className="z-50 max-h-[var(--radix-select-content-available-height)] min-w-[var(--radix-select-trigger-width)] max-w-[calc(100vw-24px)] overflow-hidden rounded-lg border border-border-strong bg-surface-raised text-text shadow-md"
        >
          <Select.ScrollUpButton className="flex justify-center py-1 text-subtle">
            <ChevronUp size={14} />
          </Select.ScrollUpButton>
          <Select.Viewport className="max-h-72 p-1">
            <Select.Group>
              <Select.Label className="px-3 py-2 text-[11px] font-medium uppercase tracking-wider text-subtle">
                {label}
              </Select.Label>
              {["", ...options.filter(Boolean)].map((option) => (
                <Select.Item
                  key={option || ANY}
                  value={option || ANY}
                  className="relative flex min-h-9 cursor-pointer items-center rounded-md py-2 pl-3 pr-9 text-sm outline-none data-[highlighted]:bg-accent-soft data-[highlighted]:text-accent"
                >
                  <Select.ItemText>
                    {option ? filterLabel(option) : "Any"}
                  </Select.ItemText>
                  <Select.ItemIndicator className="absolute right-3 text-accent">
                    <Check size={15} />
                  </Select.ItemIndicator>
                </Select.Item>
              ))}
            </Select.Group>
          </Select.Viewport>
          <Select.ScrollDownButton className="flex justify-center py-1 text-subtle">
            <ChevronDown size={14} />
          </Select.ScrollDownButton>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}
