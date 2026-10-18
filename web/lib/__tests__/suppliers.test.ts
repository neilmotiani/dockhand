import { describe, expect, it } from "vitest";

import { pct, problemCounts, sortRows } from "../suppliers";
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
  it("rounds to one decimal and shows k/n", () => {
    expect(pct({ value: 0.6667, k: 2, n: 3 })).toBe("66.7% (2/3)");
    expect(pct({ value: 0.3333, k: 1, n: 3 })).toBe("33.3% (1/3)");
    expect(pct({ value: 0.7778, k: 14, n: 18 })).toBe("77.8% (14/18)");
    expect(pct({ value: 0.625, k: 5, n: 8 })).toBe("62.5% (5/8)");
    // Exact halves at the second decimal round up: 1/16 = 6.25%.
    expect(pct({ value: 0.0625, k: 1, n: 16 })).toBe("6.3% (1/16)");
    expect(pct({ value: 1, k: 8, n: 8 })).toBe("100.0% (8/8)");
    expect(pct({ value: 0, k: 0, n: 4 })).toBe("0.0% (0/4)");
    expect(pct({ value: null, k: 0, n: 0 })).toBe("–");
  });
});

describe("problemCounts", () => {
  it("lists every count, zeros included", () => {
    const r = row("US Foods", {
      short_rate: { value: 0.6667, k: 4, n: 6 },
      missed: 2,
      late: 1,
      boxes_short: 11,
    });
    expect(problemCounts(r)).toBe("4 short, 2 missed, 1 late, 11 boxes short");
    expect(problemCounts(row("Sysco", { boxes_short: 1 }))).toBe(
      "0 short, 0 missed, 0 late, 1 box short",
    );
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
