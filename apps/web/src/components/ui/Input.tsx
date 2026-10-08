// shadcn/ui Input styling, with Origin UI's compact input-group treatment.
import { forwardRef, type InputHTMLAttributes } from "react";
import { Search } from "lucide-react";
import { cn } from "@/lib/cn";
export const Input = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "flex h-9 w-full min-w-0 rounded-md border border-border-strong bg-surface px-3 text-sm text-text placeholder:text-subtle focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";
export function SearchInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className="relative min-w-0">
      <Search
        size={15}
        aria-hidden
        className="pointer-events-none absolute left-3 top-3 text-subtle"
      />
      <Input {...props} className={cn("pl-9", props.className)} type="search" />
    </div>
  );
}
