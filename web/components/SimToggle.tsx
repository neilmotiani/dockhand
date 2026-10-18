"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";

/** "Show simulated data" switch. The server page works out the target URL (lib/sim.ts). */
export function SimToggle({ show, href }: { show: boolean; href: string }) {
  const router = useRouter();
  const [busy, start] = useTransition();
  return (
    <label className="sim-toggle" aria-busy={busy}>
      <input
        type="checkbox"
        role="switch"
        checked={show}
        onChange={() => start(() => router.push(href, { scroll: false }))}
      />
      Show simulated data
    </label>
  );
}
