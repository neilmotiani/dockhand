"use client";

import { useState } from "react";

import { SimulatedBadge } from "@/components/SimulatedBadge";
import { pct, sortRows, type SortDir, type SortKey } from "@/lib/suppliers";
import type { SupplierRow } from "@/lib/types";

const COLUMNS: { key: SortKey; label: string; num?: boolean; title: string }[] = [
  { key: "supplier", label: "Supplier", title: "Supplier name on the order" },
  { key: "due", label: "Orders due", num: true, title: "Orders whose window plus grace is over" },
  {
    key: "on_time",
    label: "On time",
    num: true,
    title: "Delivered by the end of the window, out of orders due (missing counts as late)",
  },
  {
    key: "short_rate",
    label: "Short rate",
    num: true,
    title: "Deliveries with fewer boxes than ordered, out of orders delivered",
  },
  { key: "late", label: "Late", num: true, title: "Delivered after the window closed" },
  { key: "missed", label: "Missed", num: true, title: "Orders that never arrived" },
  {
    key: "boxes_short",
    label: "Boxes short",
    num: true,
    title: "Total boxes missing across short deliveries",
  },
];

// Sorting starts "worst first" for the numbers where higher is worse.
const FIRST_DIR: Record<SortKey, SortDir> = {
  supplier: "asc",
  due: "desc",
  on_time: "asc",
  short_rate: "desc",
  late: "desc",
  missed: "desc",
  boxes_short: "desc",
  problem_rate: "desc",
};

export function SupplierTable({ rows }: { rows: SupplierRow[] }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({
    key: "problem_rate",
    dir: "desc",
  });
  const sorted = sortRows(rows, sort.key, sort.dir);
  const click = (key: SortKey) =>
    setSort((s) =>
      s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: FIRST_DIR[key] },
    );

  return (
    <table>
      <thead>
        <tr>
          {COLUMNS.map((c) => {
            const active = sort.key === c.key;
            return (
              <th
                key={c.key}
                className={`sortable${c.num ? " num" : ""}`}
                aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
              >
                <button type="button" onClick={() => click(c.key)} title={c.title}>
                  {c.label}
                  <span aria-hidden>{active ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}</span>
                </button>
              </th>
            );
          })}
        </tr>
      </thead>
      <tbody>
        {sorted.map((r) => (
          <tr key={r.supplier} className={r.worst ? "worst" : undefined}>
            <td>
              <div className="badge-stack">
                <strong>{r.supplier}</strong>
                {r.worst && (
                  <span className="badge s-missing">
                    <span className="badge-icon" aria-hidden>
                      !
                    </span>
                    Worst supplier
                  </span>
                )}
                {r.simulated_orders > 0 && (
                  <span className="small muted" style={{ display: "flex", gap: 6 }}>
                    <SimulatedBadge />
                    {r.simulated_orders === r.orders
                      ? "all orders"
                      : `${r.simulated_orders} of ${r.orders} orders`}
                  </span>
                )}
              </div>
            </td>
            <td className="num big-num">
              {r.due}
              {r.pending > 0 && <div className="small muted">+{r.pending} open</div>}
            </td>
            <td className="num">{pct(r.on_time)}</td>
            <td className="num">{pct(r.short_rate)}</td>
            <td className="num big-num">{r.late}</td>
            <td className="num big-num">{r.missed}</td>
            <td className="num big-num">{r.boxes_short}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
