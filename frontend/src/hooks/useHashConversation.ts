import { useCallback, useEffect, useState } from "react";

// The open conversation lives in the URL ("#/c/<id>"), so a reload or a shared link reopens it.
const PATTERN = /^#\/c\/([A-Za-z0-9_-]+)$/;

function readHash(): string | null {
  return PATTERN.exec(window.location.hash)?.[1] ?? null;
}

export function useHashConversation(): [string | null, (id: string | null) => void] {
  const [id, setIdState] = useState<string | null>(readHash);

  useEffect(() => {
    const onChange = () => setIdState(readHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const setId = useCallback((next: string | null) => {
    const hash = next ? `#/c/${next}` : "";
    if (window.location.hash !== hash) {
      history.pushState(null, "", hash || window.location.pathname + window.location.search);
    }
    setIdState(next);
  }, []);

  return [id, setId];
}
