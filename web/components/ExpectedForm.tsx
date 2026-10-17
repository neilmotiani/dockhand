"use client";

import { useActionState } from "react";

import { addExpected, type ExpectedFormState } from "@/app/actions";

const CARRIERS = ["UPS", "FedEx", "USPS", "Amazon", "DHL", "Sysco", "US Foods", "Other"];

/** Add an expected delivery for `date`. Times are wall-clock times in the business timezone;
 * the server action turns them into offset timestamps for POST /v1/expected. */
export function ExpectedForm({
  date,
  timezone,
  initialId,
}: {
  date: string;
  timezone: string;
  initialId: string;
}) {
  const [state, action, pending] = useActionState<ExpectedFormState, FormData>(addExpected, {
    ok: true,
    message: "",
    at: 0,
    nextId: initialId,
  });
  // After a failure the action returns what was typed (and the same order ID); after a
  // success the fields go back to their defaults for the next order.
  const v = state.values ?? {};
  return (
    <form action={action}>
      <input type="hidden" name="id" value={state.nextId} />
      <input type="hidden" name="date" value={date} />
      <input type="hidden" name="timezone" value={timezone} />
      <div className="form-grid">
        <label className="field">
          Supplier
          <input
            name="supplier"
            required
            maxLength={200}
            placeholder="e.g. Sysco"
            defaultValue={v.supplier}
          />
        </label>
        <label className="field">
          Carrier
          <select name="carrier" defaultValue={v.carrier ?? ""}>
            <option value="">Any / not sure</option>
            {CARRIERS.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
        <label className="field">
          Boxes
          <input
            name="expected_count"
            type="number"
            min={1}
            max={10000}
            defaultValue={v.expected_count ?? 1}
            required
          />
        </label>
        <label className="field">
          From
          <input name="start" type="time" defaultValue={v.start ?? "09:00"} required />
        </label>
        <label className="field">
          To
          <input name="end" type="time" defaultValue={v.end ?? "12:00"} required />
        </label>
        <label className="field">
          PO / reference
          <input
            name="reference"
            maxLength={200}
            placeholder="optional"
            defaultValue={v.reference}
          />
        </label>
        <button className="btn btn-primary btn-lg" disabled={pending}>
          {pending ? "Adding…" : "Add order"}
        </button>
      </div>
      {state.message && (
        <div className={`notice ${state.ok ? "notice-ok" : "notice-err"}`} role="status">
          {state.message}
        </div>
      )}
    </form>
  );
}
