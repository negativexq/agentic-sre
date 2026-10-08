/** Only the local incident explorer is accepted as a return destination. */
export function incidentReturn(value: string | null): string {
  if (!value) return "/incidents";
  try {
    const url = new URL(value, "https://console.local");
    return url.origin === "https://console.local" &&
      url.pathname === "/incidents"
      ? `${url.pathname}${url.search}`
      : "/incidents";
  } catch {
    return "/incidents";
  }
}

export function incidentLink(id: string, pathname: string, search: string) {
  const path = `/incidents/${encodeURIComponent(id)}`;
  return pathname === "/incidents"
    ? `${path}?${new URLSearchParams({ returnTo: pathname + search })}`
    : path;
}
