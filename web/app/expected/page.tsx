import { randomUUID } from "node:crypto";

import { ApiProblem } from "@/components/ApiProblem";
import { AutoRefresh } from "@/components/AutoRefresh";
import { DayNav } from "@/components/DayNav";
import { ExpectedForm } from "@/components/ExpectedForm";
import { StatusBadge } from "@/components/StatusBadge";
import { apiGet, withQuery } from "@/lib/api";
import { formatTime } from "@/lib/time";
import type { Day, LedgerExpected } from "@/lib/types";

export default async function ExpectedPage({ searchParams }: PageProps<"/expected">) {
  const { date } = await searchParams;
  let data;
  try {
    data = await apiGet<Day & { expected: LedgerExpected[] }>(withQuery("/v1/expected", date));
  } catch (err) {
    return <ApiProblem error={err} />;
  }
  const tz = data.timezone;
  const rows = data.expected;

  return (
    <>
      {data.date === data.today && <AutoRefresh />}
      <div className="page-head">
        <div>
          <h1>Expected deliveries</h1>
          <p>
            Tell Dockhand what is coming. Each arrival is matched to these orders automatically.
          </p>
        </div>
        <DayNav path="/expected" date={data.date} today={data.today} />
      </div>

      <div className="stack">
        <section className="card card-pad">
          <h2 style={{ marginBottom: 14 }}>Add an order</h2>
          <ExpectedForm date={data.date} timezone={tz} initialId={randomUUID()} />
          <p className="small muted" style={{ margin: "10px 0 0" }}>
            Times are in {tz.replace("_", " ")}. An order turns <strong>missing</strong> if nothing
            matches it by the end of its window plus the grace period.
          </p>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>Orders</h2>
            <span className="small muted">{rows.length} expected</span>
          </div>
          {rows.length === 0 ? (
            <div className="empty">No orders for this day yet.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Supplier</th>
                  <th>Carrier</th>
                  <th>Window</th>
                  <th className="num">Expected</th>
                  <th className="num">Received</th>
                  <th>Status</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <strong>{r.supplier}</strong>
                      {r.reference && <div className="small muted">{r.reference}</div>}
                    </td>
                    <td>{r.carrier ?? <span className="muted">Any</span>}</td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {formatTime(r.window_start, tz)} – {formatTime(r.window_end, tz)}
                    </td>
                    <td className="num big-num">{r.expected_count}</td>
                    <td className="num big-num">
                      {r.delivery ? r.delivery.package_count : <span className="muted">–</span>}
                    </td>
                    <td>
                      <StatusBadge status={r.status} />
                    </td>
                    <td className="reason">
                      {r.reason ?? "Waiting for the delivery window to close."}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </>
  );
}
