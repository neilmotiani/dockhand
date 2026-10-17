"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** Re-render the page's server components every `ms` while the tab is visible, and at once
 * when it becomes visible again. The data is fetched server-side on each refresh, so the
 * browser never talks to the API directly. */
export function AutoRefresh({ ms = 3000 }: { ms?: number }) {
  const router = useRouter();
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === "visible") router.refresh();
    }, ms);
    const onVisible = () => {
      if (document.visibilityState === "visible") router.refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [router, ms]);
  return (
    <span className="small muted" title={`Refreshes every ${ms / 1000} s`}>
      <span className="live-dot" aria-hidden />
      Live
    </span>
  );
}
