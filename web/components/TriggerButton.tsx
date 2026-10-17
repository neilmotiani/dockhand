"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";

import { type ActionResult, triggerCapture } from "@/app/actions";

/** Demo control: start a capture as if the Ring camera had just seen someone at the door
 * (POST /v1/trigger). The ledger picks the event up on its next refresh. */
export function TriggerButton() {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [result, setResult] = useState<ActionResult | null>(null);

  useEffect(() => {
    if (!result) return;
    const id = setTimeout(() => setResult(null), 6000);
    return () => clearTimeout(id);
  }, [result]);

  return (
    <>
      <button
        type="button"
        className="btn btn-primary btn-lg"
        disabled={pending}
        onClick={() =>
          start(async () => {
            const r = await triggerCapture();
            setResult(r);
            if (r.ok) router.push("/");
          })
        }
      >
        <span aria-hidden>{pending ? "◷" : "●"}</span>
        {pending ? "Starting…" : "Simulate delivery"}
      </button>
      {result && (
        <div className={`toast notice ${result.ok ? "notice-ok" : "notice-err"}`} role="status">
          {result.message}
        </div>
      )}
    </>
  );
}
