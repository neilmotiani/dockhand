"use client";

import { useState } from "react";

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <button
      type="button"
      className="btn btn-primary"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setState("copied");
        } catch {
          // Clipboard needs a secure context (https or localhost) and permission.
          setState("failed");
        }
        setTimeout(() => setState("idle"), 2500);
      }}
    >
      <span aria-hidden>{state === "copied" ? "✓" : "⧉"}</span>
      {state === "copied" ? "Copied" : state === "failed" ? "Copy blocked; select the text" : label}
    </button>
  );
}
