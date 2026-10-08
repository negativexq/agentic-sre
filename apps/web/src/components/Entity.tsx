/** Keep the full wire identity accessible, including namespace and resource kind. */
export function Entity({ value }: { value: string }) {
  const parts = value.split("/");
  const kind = parts.length >= 2 ? parts[parts.length - 2] : null;
  const namespace = parts.length >= 3 ? parts.slice(0, -2).join("/") : null;
  return (
    <span className="block min-w-0" title={value}>
      {kind && (
        <span className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-subtle">
          {namespace ? `${namespace} / ` : ""}
          {kind}
        </span>
      )}
      <code className="break-anywhere text-xs text-text">
        {parts.length >= 2 ? parts[parts.length - 1] : value}
      </code>
    </span>
  );
}
