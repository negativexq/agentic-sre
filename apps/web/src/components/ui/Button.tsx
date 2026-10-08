// shadcn/ui-style variants; existing public API retained for operator workflows.
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cva } from "class-variance-authority";
import { cn } from "@/lib/cn";
const variants = cva(
  "inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        primary:
          "border border-accent bg-accent text-accent-fg hover:opacity-90",
        secondary:
          "border border-border-strong bg-surface text-text hover:bg-surface-raised",
        ghost:
          "border border-transparent text-muted hover:bg-surface hover:text-text",
      },
    },
    defaultVariants: { variant: "secondary" },
  },
);
export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: "primary" | "secondary" | "ghost";
    children: ReactNode;
  }
>(({ variant = "secondary", className, children, ...props }, ref) => (
  <button
    ref={ref}
    className={cn(variants({ variant }), "px-3 py-1.5", className)}
    {...props}
  >
    {children}
  </button>
));
Button.displayName = "Button";
