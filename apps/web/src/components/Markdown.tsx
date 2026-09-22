import { Fragment, type ReactNode } from "react";

/**
 * A deliberately small Markdown renderer for report previews: headings, bold,
 * inline code, bullet lists, blockquotes and pipe tables. It is not a general
 * Markdown engine — it renders exactly the shapes the report renderer emits.
 */
function inline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) {
      nodes.push(
        <strong key={`${keyBase}-b${i}`} className="font-semibold text-text">
          {token.slice(2, -2)}
        </strong>,
      );
    } else {
      nodes.push(
        <code key={`${keyBase}-c${i}`} className="rounded bg-surface px-1 font-mono text-xs">
          {token.slice(1, -1)}
        </code>,
      );
    }
    last = match.index + token.length;
    i += 1;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function Row({ cells, header }: { cells: string[]; header: boolean }) {
  const Cell = header ? "th" : "td";
  return (
    <tr className="border-b border-border/60">
      {cells.map((cell, index) => (
        <Cell key={index} className="px-2 py-1 text-left align-top text-muted">
          {inline(cell.trim(), `r${index}`)}
        </Cell>
      ))}
    </tr>
  );
}

export function Markdown({ source }: { source: string }) {
  const lines = source.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("# ")) {
      blocks.push(
        <h1 key={i} className="mt-2 text-lg font-semibold text-text break-anywhere">
          {inline(line.slice(2), `h1-${i}`)}
        </h1>,
      );
    } else if (line.startsWith("## ")) {
      blocks.push(
        <h2 key={i} className="mt-4 text-sm font-semibold uppercase tracking-wide text-subtle">
          {inline(line.slice(3), `h2-${i}`)}
        </h2>,
      );
    } else if (line.startsWith("### ")) {
      blocks.push(
        <h3 key={i} className="mt-3 text-sm font-medium text-text">
          {inline(line.slice(4), `h3-${i}`)}
        </h3>,
      );
    } else if (line.startsWith("> ")) {
      blocks.push(
        <blockquote key={i} className="border-l-2 border-accent pl-3 text-sm text-muted italic">
          {inline(line.slice(2), `q-${i}`)}
        </blockquote>,
      );
    } else if (line.startsWith("- ")) {
      const items: string[] = [];
      while (i < lines.length && lines[i].startsWith("- ")) {
        items.push(lines[i].slice(2));
        i += 1;
      }
      blocks.push(
        <ul key={i} className="list-disc space-y-0.5 pl-5 text-sm text-muted">
          {items.map((item, index) => (
            <li key={index} className="break-anywhere">
              {inline(item, `li-${i}-${index}`)}
            </li>
          ))}
        </ul>,
      );
      continue;
    } else if (line.startsWith("| ")) {
      const rows: string[] = [];
      while (i < lines.length && lines[i].startsWith("| ")) {
        rows.push(lines[i]);
        i += 1;
      }
      const parsed = rows
        .filter((row) => !/^\|[\s|:-]+\|$/.test(row.trim()))
        .map((row) => row.slice(1, row.lastIndexOf("|")).split("|"));
      blocks.push(
        <table key={i} className="my-1 w-full text-xs">
          <tbody>
            {parsed.map((cells, index) => (
              <Row key={index} cells={cells} header={index === 0} />
            ))}
          </tbody>
        </table>,
      );
      continue;
    } else if (line.trim() !== "") {
      blocks.push(
        <p key={i} className="text-sm text-muted break-anywhere">
          {inline(line, `p-${i}`)}
        </p>,
      );
    }
    i += 1;
  }

  return <div className="space-y-2">{blocks.map((block, index) => <Fragment key={index}>{block}</Fragment>)}</div>;
}
