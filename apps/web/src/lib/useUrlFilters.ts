import { useSearchParams } from "react-router-dom";

/** View state only. Preserve unrelated URL parameters and backend wire values. */
export function useUrlFilters() {
  const [params, setParams] = useSearchParams();
  const update = (patch: Record<string, string | null>) => {
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        for (const [key, value] of Object.entries(patch)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        return next;
      },
      { replace: true },
    );
  };
  return { params, update };
}
