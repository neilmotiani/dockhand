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

/** "66.7% (2/3)", or "–" with no denominator. Rounded to one decimal, computed from k/n
 * rather than the API's 4-decimal value. For ledger figures only: the eval and bench metrics
 * on How it works keep their own formatting. */
export function pct(r: RatioOrNull): string {
  if (!r.n) return "–";
  // Integer maths before dividing, so 2/3 -> 667 -> 66.7 with no binary-float surprises.
  return `${(Math.round((r.k * 1000) / r.n) / 10).toFixed(1)}% (${r.k}/${r.n})`;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "4 short, 2 missed, 1 late, 11 boxes short": every count, zeros included, so the banner
 * reads the same for every supplier. */
export function problemCounts(r: SupplierRow): string {
  return [
    `${r.short_rate.k} short`,
    `${r.missed} missed`,
    `${r.late} late`,
    `${plural(r.boxes_short, "box", "boxes")} short`,
  ].join(", ");
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
