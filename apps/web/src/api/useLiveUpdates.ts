import { useEffect, useState } from "react";
import { useQueryClient, type QueryKey } from "@tanstack/react-query";

const BASE = "/api/v1/console";

export type LiveState = "connecting" | "live" | "offline";

/**
 * Subscribe to a console SSE stream and invalidate the given query keys on each
 * persisted-state signal. The stream only tells us *that* something changed;
 * the refetch pulls the authoritative DTO, so the store stays the source of
 * truth. EventSource reconnects on its own; on reconnect we invalidate again to
 * reconcile whatever was missed while offline.
 */
export function useLiveUpdates(path: string, keys: QueryKey[]): LiveState {
  const queryClient = useQueryClient();
  const [state, setState] = useState<LiveState>("connecting");

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      setState("offline");
      return;
    }
    const source = new EventSource(`${BASE}${path}`);
    const invalidate = () => {
      setState("live");
      for (const key of keys) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
    };
    source.onopen = () => setState("live");
    source.onmessage = invalidate;
    source.addEventListener("state", invalidate);
    source.addEventListener("incident", invalidate);
    source.onerror = () => setState("connecting");

    return () => source.close();
    // keys is recreated per render; callers pass stable-enough arrays. The path
    // is the real subscription identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, queryClient]);

  return state;
}
