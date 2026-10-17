import { describe, expect, it } from "vitest";

import { formatDay, formatTime, seconds, shiftDay, zonedToIso } from "../time";

const LA = "America/Los_Angeles";

describe("zonedToIso", () => {
  it("applies the zone's offset", () => {
    expect(zonedToIso("2026-10-17", "09:30", LA)).toBe("2026-10-17T09:30:00-07:00");
    expect(zonedToIso("2026-12-01", "09:30", LA)).toBe("2026-12-01T09:30:00-08:00");
    expect(zonedToIso("2026-10-17", "09:30", "Asia/Kolkata")).toBe("2026-10-17T09:30:00+05:30");
    expect(zonedToIso("2026-10-17", "00:00", "UTC")).toBe("2026-10-17T00:00:00+00:00");
  });

  it("round-trips to the same instant", () => {
    const iso = zonedToIso("2026-10-17", "14:05", LA);
    expect(new Date(iso).toISOString()).toBe("2026-10-17T21:05:00.000Z");
  });

  it("handles DST transition days", () => {
    // 2026-11-01: clocks fall back at 02:00 PDT. 12:00 that day is PST; 01:30 happens twice
    // and the earlier one (PDT) is used.
    expect(zonedToIso("2026-11-01", "12:00", LA)).toBe("2026-11-01T12:00:00-08:00");
    expect(zonedToIso("2026-11-01", "01:30", LA)).toBe("2026-11-01T01:30:00-07:00");
    // 2026-03-08: 02:30 does not exist; it lands an hour later, as 03:30 PDT.
    expect(zonedToIso("2026-03-08", "02:30", LA)).toBe("2026-03-08T03:30:00-07:00");
  });

  it("rejects garbage", () => {
    expect(() => zonedToIso("2026-10-17", "", LA)).toThrow();
  });
});

describe("formatting", () => {
  it("shows times in the business timezone", () => {
    expect(formatTime("2026-10-16T17:00:00+00:00", LA)).toBe("10:00 AM");
    expect(formatTime("2026-10-16T17:00:05+00:00", LA, true)).toBe("10:00:05 AM");
  });

  it("formats and shifts calendar days without timezone drift", () => {
    expect(formatDay("2026-10-17")).toBe("Sat, Oct 17, 2026");
    expect(shiftDay("2026-10-31", 1)).toBe("2026-11-01");
    expect(shiftDay("2026-03-01", -1)).toBe("2026-02-28");
  });

  it("formats durations", () => {
    expect(seconds(12510.7)).toBe("12.5 s");
    expect(seconds(null)).toBe("–");
  });
});
