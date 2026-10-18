import { ApiProblem } from "@/components/ApiProblem";
import { SimToggle } from "@/components/SimToggle";
import { SupplierTable } from "@/components/SupplierTable";
import { apiGet, withQuery } from "@/lib/api";
import { showSimulated, simApiQuery, simPageQuery, toggleHref } from "@/lib/sim";
import { pct, problemCounts } from "@/lib/suppliers";
import { formatDay } from "@/lib/time";
import type { Day, Suppliers } from "@/lib/types";

export default async function SuppliersPage({ searchParams }: PageProps<"/suppliers">) {
  const { sim } = await searchParams;
  const show = showSimulated(sim);
  let data;
  try {
    data = await apiGet<Day & Suppliers>(
      withQuery("/v1/suppliers", undefined, { days: "30", ...simApiQuery(show) }),
    );
  } catch (err) {
    return <ApiProblem error={err} />;
  }
  const worst = data.suppliers.find((s) => s.worst);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Suppliers</h1>
          <p>
            How reliable each supplier has been over the last {data.days} days (
            {formatDay(data.from)} to {formatDay(data.to)}), from the delivery ledger.
          </p>
        </div>
        <div className="head-tools">
          <SimToggle show={show} href={toggleHref("/suppliers", simPageQuery(show), show)} />
        </div>
      </div>

      {data.includes_simulated && (
        <div className="card card-pad notice-sim" style={{ marginBottom: 20 }} role="note">
          <strong>Includes simulated data.</strong> {data.simulated_orders} of {data.orders} orders
          in these figures are simulated demo history, not real deliveries. Turn off{" "}
          <em>Show simulated data</em> to see real orders only.
        </div>
      )}

      {worst && (
        <div className="worst-callout">
          <strong>{worst.supplier}</strong> had the most problems: {problemCounts(worst)} (
          {worst.problem_rate.k} of {worst.due} orders due had a problem). On time{" "}
          {pct(worst.on_time)}.
        </div>
      )}

      <section className="card">
        <div className="card-head">
          <h2>Last {data.days} days</h2>
          <span className="small muted">{data.orders} orders · click a column to sort</span>
        </div>
        {data.suppliers.length === 0 ? (
          <div className="empty">No orders in the last {data.days} days.</div>
        ) : (
          <SupplierTable rows={data.suppliers} />
        )}
      </section>
      <p className="small muted" style={{ marginTop: 12 }}>
        On time: delivered by the end of the order&apos;s window, out of orders due (a missed order
        counts as not on time). Short rate: deliveries with fewer boxes than ordered, out of orders
        delivered. Orders still inside their window plus grace are not counted yet. These are ledger
        figures for your business, not Dockhand accuracy metrics.
      </p>
    </>
  );
}
