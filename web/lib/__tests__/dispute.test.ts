import { describe, expect, it } from "vitest";

import { disputeSummary, localDate } from "../dispute";
import type { Delivery, Discrepancy, Expected } from "../types";

const LA = "America/Los_Angeles";

const expected: Expected = {
  id: "e1",
  supplier: "Sysco",
  carrier: "UPS",
  expected_count: 3,
  window_start: "2026-10-16T16:00:00+00:00",
  window_end: "2026-10-16T18:00:00+00:00",
  reference: "PO-1042",
};

const delivery: Delivery = {
  id: "d1",
  event_id: "ev1",
  device_id: "dev",
  detected_at: "2026-10-16T17:12:00+00:00",
  carrier: "UPS",
  package_count: 1,
  label_text: [],
  confidence: 0.8,
  source: "ring_playground",
  model_id: "m",
  prompt_version: "v1",
  frames_sampled: 10,
  frames_sent: 3,
  event_to_ledger_ms: 12500,
  media: { snapshot: "/v1/deliveries/d1/snapshot", frames: [], clip: null },
};

function disc(over: Partial<Discrepancy>): Discrepancy {
  return {
    id: "r1",
    status: "short",
    reason: "Short by 2",
    decided_at: "2026-10-16T17:13:00+00:00",
    delivery,
    expected,
    ...over,
  };
}

describe("disputeSummary", () => {
  it("short: states ordered, received and the gap", () => {
    const text = disputeSummary(disc({}), LA, "Maria's Café");
    expect(text).toContain("Subject: Short delivery – Sysco PO-1042");
    expect(text).toContain(
      "We received 1 box for order PO-1042 on Fri, Oct 16, 2026 at 10:12 AM, but 3 boxes were expected. We are short 2 boxes.",
    );
    expect(text).toContain("snapshot is attached");
    expect(text.trim().endsWith("Maria's Café")).toBe(true);
  });

  it("missing: gives the window and has no evidence line", () => {
    const text = disputeSummary(disc({ status: "missing", delivery: null }), LA);
    expect(text).toContain("between 9:00 AM and 11:00 AM");
    expect(text).toContain("recorded no matching delivery");
    expect(text).not.toContain("snapshot");
  });

  it("unexpected: names the carrier and label text, no supplier", () => {
    const text = disputeSummary(
      disc({
        status: "unexpected",
        expected: null,
        delivery: { ...delivery, carrier: "Amazon", package_count: 2, label_text: ["TBA123"] },
      }),
      LA,
    );
    expect(text).toContain("We received an Amazon delivery of 2 boxes");
    expect(text).toContain("Label text read from the boxes: TBA123.");
    expect(text).not.toContain("Sysco");
  });

  it("unexpected with unreadable carrier", () => {
    const text = disputeSummary(
      disc({ status: "unexpected", expected: null, delivery: { ...delivery, carrier: "Unknown" } }),
      LA,
    );
    expect(text).toContain("We received a delivery of 1 box");
  });

  it("over: asks about billing", () => {
    const text = disputeSummary(
      disc({ status: "over", delivery: { ...delivery, package_count: 5 } }),
      LA,
    );
    expect(text).toContain("5 boxes for order PO-1042");
    expect(text).toContain("only 3 boxes were ordered (2 extra)");
  });

  it("without a reference says 'our order'", () => {
    const text = disputeSummary(disc({ expected: { ...expected, reference: null } }), LA);
    expect(text).toContain("for our order on");
    expect(text).toContain("Subject: Short delivery – Sysco\n");
  });

  it("falls back to the stored reason when a side is missing", () => {
    expect(disputeSummary(disc({ delivery: null }), LA)).toBe("Short by 2");
  });

  it("never leaves a double blank line", () => {
    for (const status of ["short", "over", "missing", "unexpected"] as const) {
      expect(disputeSummary(disc({ status }), LA)).not.toMatch(/\n\n\n/);
    }
  });
});

describe("localDate", () => {
  it("uses the business day, not UTC", () => {
    expect(localDate("2026-10-17T05:30:00+00:00", LA)).toBe("2026-10-16");
  });
});
