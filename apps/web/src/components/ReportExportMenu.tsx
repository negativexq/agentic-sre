// shadcn-style Dropdown Menu; export links retain the existing download API.
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Download, ChevronDown, FileJson, FileText } from "lucide-react";
import { reportUrl } from "@/api/client";

export function ReportExportMenu({ reportId }: { reportId: string }) {
  return (
    <Menu.Root>
      <Menu.Trigger
        className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-xs text-muted hover:bg-surface hover:text-text"
        aria-label="Export report"
      >
        <Download size={13} aria-hidden /> Export{" "}
        <ChevronDown size={12} aria-hidden />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content
          align="end"
          sideOffset={5}
          collisionPadding={12}
          className="z-50 min-w-44 rounded-lg border border-border-strong bg-surface-raised p-1 text-text shadow-md"
        >
          {(["pdf", "markdown", "json"] as const).map((format) => (
            <Menu.Item key={format} asChild>
              <a
                href={reportUrl(reportId, format)}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-2 rounded-md px-3 py-2 text-sm outline-none data-[highlighted]:bg-accent-soft data-[highlighted]:text-accent"
              >
                {format === "json" ? (
                  <FileJson size={15} aria-hidden />
                ) : (
                  <FileText size={15} aria-hidden />
                )}
                {format === "pdf"
                  ? "PDF document"
                  : format === "markdown"
                    ? "Markdown"
                    : "JSON snapshot"}
              </a>
            </Menu.Item>
          ))}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}
