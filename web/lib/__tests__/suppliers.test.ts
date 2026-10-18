import { describe, expect, it } from "vitest";

import { pct, sortRows } from "../suppliers";
import type { SupplierRow } from "../types";

function row(supplier: string, over: Partial<SupplierRow> = {}): SupplierRow {
  const r = { value: 1, k: 1, n: 1 };
  return {
    supplier,
    orders: 1,
    pending: 0,
    due: 1,
    delivered: 1,
    on_time: r,
    late: 0,
    short_rate: { value: 0, k: 0, n: 1 },
    over: 0,
    missed: 0,
    boxes_short: 0,
    problem_rate: { value: 0, k: 0, n: 1 },
    simulated_orders: 0,
    last_order: null,
    worst: false,
    ...over,
  };
}

describe("pct", () => {
  it("shows k/n and never rounds up", () => {
    expect(pct({ value: 0.6667, k: 2, n: 3 })).toBe("66.6% (2/3)");
    expect(pct({ value: 1, k: 8, n: 8 })).toBe("100.0% (8/8)");
    expect(pct({ value: null, k: 0, n: 0 })).toBe("–");
  });
});

describe("sortRows", () => {
  const rows = [
    row("Sysco", { boxes_short: 0, on_time: { value: 1, k: 8, n: 8 } }),
    row("US Foods", { boxes_short: 11, on_time: { value: 0.625, k: 5, n: 8 } }),
    row("New", { on_time: { value: null, k: 0, n: 0 } }),
  ];

  it("sorts numbers both ways, ties by name", () => {
    expect(sortRows(rows, "boxes_short", "desc").map((r) => r.supplier)).toEqual([
      "US Foods",
      "New",
      "Sysco",
    ]);
    expect(sortRows(rows, "supplier", "asc").map((r) => r.supplier)).toEqual([
      "New",
      "Sysco",
      "US Foods",
    ]);
  });

  it("puts rows with no denominator last in either direction", () => {
    expect(sortRows(rows, "on_time", "asc").map((r) => r.supplier)).toEqual([
      "US Foods",
      "Sysco",
      "New",
    ]);
    expect(sortRows(rows, "on_time", "desc").map((r) => r.supplier)).toEqual([
      "Sysco",
      "US Foods",
      "New",
    ]);
  });
});
