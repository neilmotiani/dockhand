import type { RatioOrNull, SupplierRow } from "./types";

export type SortKey =
  | "supplier"
  | "due"
  | "on_time"
  | "short_rate"
  | "late"
  | "missed"
  | "boxes_short"
  | "problem_rate";

export type SortDir = "asc" | "desc";

/** "62.5% (5/8)", or "–" with no denominator. Never rounded up past one decimal. */
export function pct(r: RatioOrNull): string {
  if (r.value == null || !r.n) return "–";
  return `${(Math.floor(r.value * 1000) / 10).toFixed(1)}% (${r.k}/${r.n})`;
}

function key(row: SupplierRow, k: SortKey): number | string {
  switch (k) {
    case "supplier":
      return row.supplier.toLowerCase();
    case "on_time":
    case "short_rate":
    case "problem_rate":
      // Rows without a denominator sort last either way.
      return row[k].value ?? Number.NaN;
    default:
      return row[k];
  }
}

export function sortRows(rows: SupplierRow[], k: SortKey, dir: SortDir): SupplierRow[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = key(a, k);
    const y = key(b, k);
    if (typeof x === "number" && typeof y === "number") {
      if (Number.isNaN(x) || Number.isNaN(y)) {
        if (Number.isNaN(x) && Number.isNaN(y)) return a.supplier.localeCompare(b.supplier);
        return Number.isNaN(x) ? 1 : -1;
      }
      if (x !== y) return sign * (x - y);
    } else if (x !== y) {
      return sign * String(x).localeCompare(String(y));
    }
    return a.supplier.localeCompare(b.supplier);
  });
}
