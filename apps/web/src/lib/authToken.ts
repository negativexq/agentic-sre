// The optional API bearer token lives only in this browser session — never in
// the bundle, env, or localStorage. The operator pastes it in Settings; the API
// client attaches it to write requests. It is cleared when the tab closes.

const KEY = "agentic-sre-api-token";

export function getToken(): string {
  try {
    return sessionStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(KEY, token);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable */
  }
  window.dispatchEvent(new Event("api-token-changed"));
}

export function clearToken(): void {
  setToken("");
}

/** Authorization header for write requests, empty when no token is set. */
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
