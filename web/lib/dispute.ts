import { formatDay, formatTime } from "./time";
import type { Discrepancy } from "./types";

// The plain-text note a business owner pastes into an email to the supplier. Facts only:
// what was ordered, what the camera logged, when, and what we are asking for. Evidence is
// referred to as an attachment because the owner attaches the snapshot themselves. The sign-off
// uses DOCKHAND_BUSINESS_NAME when it is set.

function boxes(n: number): string {
  return n === 1 ? "1 box" : `${n} boxes`;
}

function order(e: NonNullable<Discrepancy["expected"]>): string {
  return e.reference ? `order ${e.reference}` : "our order";
}

// Simulated history must never pass for a real dispute, even if someone copies it.
export const SIMULATED_NOTE = "[Simulated demo data: not a real delivery. Do not send.]";

export function disputeSummary(x: Discrepancy, timeZone: string, business?: string): string {
  const text = summaryText(x, timeZone, business);
  return x.simulated ? `${SIMULATED_NOTE}\n\n${text}` : text;
}

function summaryText(x: Discrepancy, timeZone: string, business?: string): string {
  const d = x.delivery;
  const e = x.expected;
  const at = (iso: string) =>
    `${formatDay(localDate(iso, timeZone))} at ${formatTime(iso, timeZone)}`;
  const evidence = d
    ? `Our receiving camera recorded the drop-off at ${formatTime(d.detected_at, timeZone)}; a snapshot is attached.`
    : null;

  switch (x.status) {
    case "short": {
      if (!d || !e) break;
      const gap = e.expected_count - d.package_count;
      return [
        `Subject: Short delivery – ${e.supplier}${e.reference ? ` ${e.reference}` : ""}`,
        "",
        `Hello ${e.supplier},`,
        "",
        `We received ${boxes(d.package_count)} for ${order(e)} on ${at(d.detected_at)}, but ${boxes(e.expected_count)} were expected. We are short ${boxes(gap)}.`,
        evidence,
        "",
        `Could you please send the missing ${boxes(gap)} or issue a credit?`,
        "",
        "Thank you,",
        business ?? null,
      ]
        .filter((line) => line !== null)
        .join("\n");
    }
    case "over": {
      if (!d || !e) break;
      const extra = d.package_count - e.expected_count;
      return [
        `Subject: Extra boxes delivered – ${e.supplier}${e.reference ? ` ${e.reference}` : ""}`,
        "",
        `Hello ${e.supplier},`,
        "",
        `We received ${boxes(d.package_count)} for ${order(e)} on ${at(d.detected_at)}, but only ${boxes(e.expected_count)} were ordered (${extra} extra).`,
        evidence,
        "",
        "Could you please confirm whether the extra boxes are ours, and that we will only be billed for what we ordered?",
        "",
        "Thank you,",
        business ?? null,
      ]
        .filter((line) => line !== null)
        .join("\n");
    }
    case "missing": {
      if (!e) break;
      return [
        `Subject: Delivery not received – ${e.supplier}${e.reference ? ` ${e.reference}` : ""}`,
        "",
        `Hello ${e.supplier},`,
        "",
        `We expected ${boxes(e.expected_count)} for ${order(e)} on ${formatDay(localDate(e.window_start, timeZone))} between ${formatTime(e.window_start, timeZone)} and ${formatTime(e.window_end, timeZone)}. Our receiving camera recorded no matching delivery.`,
        "",
        "Could you please check the status of this delivery and let us know when it will arrive?",
        "",
        "Thank you,",
        business ?? null,
      ]
        .filter((line) => line !== null)
        .join("\n");
    }
    case "unexpected": {
      if (!d) break;
      const named = d.carrier !== "Unknown" && d.carrier !== "Other";
      const carrier = !named
        ? "a"
        : /^[AEIOU]/.test(d.carrier)
          ? `an ${d.carrier}`
          : `a ${d.carrier}`;
      return [
        `Subject: Unexpected delivery on ${formatDay(localDate(d.detected_at, timeZone))}`,
        "",
        "Hello,",
        "",
        `We received ${carrier} delivery of ${boxes(d.package_count)} on ${at(d.detected_at)} that does not match any order we placed.`,
        evidence,
        d.label_text.length ? `Label text read from the boxes: ${d.label_text.join(", ")}.` : null,
        "",
        "Could you please tell us which order this belongs to, or arrange collection?",
        "",
        "Thank you,",
        business ?? null,
      ]
        .filter((line) => line !== null)
        .join("\n");
    }
  }
  return x.reason;
}

/** The calendar day (YYYY-MM-DD) of an instant in `timeZone`. */
export function localDate(iso: string, timeZone: string): string {
  // en-CA formats as YYYY-MM-DD.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(iso));
}
