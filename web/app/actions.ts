"use server";

import { randomUUID } from "node:crypto";

import { revalidatePath } from "next/cache";

import { ApiError, apiPost } from "@/lib/api";
import { zonedToIso } from "@/lib/time";

// Server actions: the only write path from the dashboard. They run on the dashboard server,
// which holds the admin token; the browser only ever sees the result message.

export interface ActionResult {
  ok: boolean;
  message: string;
  at: number;
}

function fail(e: unknown): ActionResult {
  const message = e instanceof ApiError ? e.message : "Something went wrong.";
  return { ok: false, message, at: Date.now() };
}

export async function triggerCapture(): Promise<ActionResult> {
  try {
    // A fresh key per click; the button is disabled while the request is in flight, so a
    // double click cannot start two captures.
    const res = await apiPost<{ status: string; event_id: string | null }>("/v1/trigger", {
      idempotency_key: `dashboard-${randomUUID()}`,
    });
    revalidatePath("/");
    return {
      ok: true,
      message:
        res.status === "duplicate"
          ? "That capture was already started."
          : "Capture started. Watching the Ring camera…",
      at: Date.now(),
    };
  } catch (e) {
    return fail(e);
  }
}

export interface ExpectedFormState extends ActionResult {
  // A new client-chosen ID for the next submission. The current ID is reused if this one
  // failed, so a retry after a timeout cannot create the order twice.
  nextId: string;
  // What was typed, sent back on failure: React resets a form after every action, and the
  // fields' defaultValues bring the input back.
  values?: Record<string, string>;
}

const CARRIERS = new Set(["UPS", "FedEx", "USPS", "Amazon", "DHL", "Sysco", "US Foods", "Other"]);

export async function addExpected(
  _prev: ExpectedFormState,
  form: FormData,
): Promise<ExpectedFormState> {
  const id = String(form.get("id") ?? "");
  const text = (k: string) => String(form.get(k) ?? "").trim();
  const values = Object.fromEntries(
    ["supplier", "carrier", "expected_count", "start", "end", "reference"].map((k) => [k, text(k)]),
  );
  const keep = (message: string): ExpectedFormState => ({
    ok: false,
    message,
    at: Date.now(),
    nextId: id || randomUUID(),
    values,
  });

  const supplier = text("supplier");
  const carrier = text("carrier");
  const count = Number(text("expected_count"));
  const date = text("date");
  const tz = text("timezone");
  if (!supplier) return keep("Supplier is required.");
  if (carrier && !CARRIERS.has(carrier)) return keep("Choose a carrier from the list.");
  if (!Number.isInteger(count) || count < 1)
    return keep("Boxes must be a whole number, 1 or more.");
  let windowStart: string, windowEnd: string;
  try {
    windowStart = zonedToIso(date, text("start"), tz);
    windowEnd = zonedToIso(date, text("end"), tz);
  } catch {
    return keep("Enter a start and end time.");
  }
  if (new Date(windowEnd) < new Date(windowStart)) {
    return keep("The window must end after it starts.");
  }

  try {
    const res = await apiPost<{
      status: "created" | "duplicate";
      reconciliation: { status: string } | null;
    }>("/v1/expected", {
      id,
      supplier,
      carrier: carrier || null,
      expected_count: count,
      window_start: windowStart,
      window_end: windowEnd,
      reference: text("reference") || null,
    });
    revalidatePath("/expected");
    revalidatePath("/");
    const now = res.reconciliation ? ` Status: ${res.reconciliation.status}.` : "";
    return {
      ok: true,
      message: `${res.status === "duplicate" ? "Already saved" : "Added"}: ${supplier}, ${count} box${count === 1 ? "" : "es"}.${now}`,
      at: Date.now(),
      nextId: randomUUID(),
    };
  } catch (e) {
    return { ...fail(e), nextId: id || randomUUID(), values };
  }
}
