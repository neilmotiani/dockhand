import { ApiProblem } from "@/components/ApiProblem";
import { AutoRefresh } from "@/components/AutoRefresh";
import { DayNav } from "@/components/DayNav";
import { Thumb } from "@/components/Media";
import { StatusBadge } from "@/components/StatusBadge";
import { apiGet, withQuery } from "@/lib/api";
import { formatTime, seconds } from "@/lib/time";
import type { Day, LedgerDelivery, LedgerExpected, PipelineEvent } from "@/lib/types";

const ORIGIN: Record<PipelineEvent["origin"], string> = {
  manual: "Demo trigger",
  poll: "Ring event",
  webhook: "Ring webhook",
};

function eventText(e: PipelineEvent): string {
  switch (e.status) {
    case "pending":
      return "Queued";
    case "processing":
      return "Capturing from Ring live view…";
    case "done":
      return e.delivery_id ? "Delivery logged" : `Not a delivery: ${e.outcome ?? ""}`;
    case "skipped":
      return `Skipped: ${e.outcome ?? ""}`;
    case "failed": {
      const why = e.outcome ?? e.last_error ?? "";
      // Playground tokens last about 30 minutes; this is the usual failure in a demo.
      const hint = why.startsWith("Ring auth") ? " (Ring token expired? Paste a fresh one.)" : "";
      return `Failed: ${why}${hint}`;
    }
    default:
      return e.status;
  }
}

export default async function TodayPage({ searchParams }: PageProps<"/">) {
  const { date } = await searchParams;
  let data;
  try {
    const [d, e, a] = await Promise.all([
      apiGet<Day & { deliveries: LedgerDelivery[] }>(withQuery("/v1/deliveries", date)),
      apiGet<Day & { expected: LedgerExpected[] }>(withQuery("/v1/expected", date)),
      apiGet<Day & { events: PipelineEvent[] }>(withQuery("/v1/activity", date, { limit: "8" })),
    ]);
    data = { d, e, a };
  } catch (err) {
    return <ApiProblem error={err} />;
  }
  const { d, e, a } = data;
  const today = d.today;
  const tz = d.timezone;
  const rows = d.deliveries;
  const inFlight = a.events.filter((x) => x.status === "pending" || x.status === "processing");
  const boxes = rows.reduce((n, r) => n + r.package_count, 0);
  const matched = rows.filter((r) => r.status === "matched").length;
  const attention =
    rows.filter((r) => ["short", "over", "unexpected"].includes(r.status)).length +
    e.expected.filter((x) => x.status === "missing").length;
  const now = new Date(d.generated_at).getTime();

  return (
    <>
      {d.date === today && <AutoRefresh />}
      <div className="page-head">
        <div>
          <h1>Delivery ledger</h1>
          <p>Every drop-off the back-door camera saw, checked against what you expected.</p>
        </div>
        <DayNav path="/" date={d.date} today={today} />
      </div>

      <div className="tiles">
        <div className="card tile">
          <div className="tile-label">Deliveries logged</div>
          <div className="tile-value">{rows.length}</div>
        </div>
        <div className="card tile">
          <div className="tile-label">Boxes received</div>
          <div className="tile-value">{boxes}</div>
        </div>
        <div className="card tile">
          <div className="tile-label">Matched to an order</div>
          <div className="tile-value">
            {matched}
            <span className="muted" style={{ fontSize: 20 }}>
              {" "}
              / {e.expected.length} expected
            </span>
          </div>
        </div>
        <div className={`card tile${attention ? " tile-alert" : ""}`}>
          <div className="tile-label">Need attention</div>
          <div className="tile-value">{attention}</div>
        </div>
      </div>

      <div className="stack">
        <section className="card">
          <div className="card-head">
            <h2>Deliveries</h2>
            <span className="small muted">Times in {tz.replace("_", " ")}</span>
          </div>
          {rows.length === 0 && inFlight.length === 0 ? (
            <div className="empty">
              No deliveries yet {d.date === today ? "today" : "on this day"}. Press{" "}
              <strong>Simulate delivery</strong> to run the Ring camera now.
            </div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Snapshot</th>
                  <th>Time</th>
                  <th>Carrier</th>
                  <th className="num">Boxes</th>
                  <th>Status</th>
                  <th>Order and reason</th>
                </tr>
              </thead>
              <tbody>
                {inFlight.map((x) => (
                  <tr key={x.id}>
                    <td>
                      <div className="thumb-empty">
                        <span className="spin" aria-hidden>
                          ◷
                        </span>
                      </div>
                    </td>
                    <td className="big-num">{formatTime(x.received_at, tz)}</td>
                    <td className="muted">–</td>
                    <td className="num muted">–</td>
                    <td>
                      <StatusBadge status="pending" />
                    </td>
                    <td className="reason">
                      {ORIGIN[x.origin]}: {eventText(x)}
                    </td>
                  </tr>
                ))}
                {rows.map((r) => (
                  <tr
                    key={r.id}
                    className={now - new Date(r.detected_at).getTime() < 45_000 ? "row-new" : ""}
                  >
                    <td>
                      <Thumb
                        src={r.media.snapshot}
                        alt={`Delivery at ${formatTime(r.detected_at, tz)}`}
                      />
                    </td>
                    <td className="big-num">{formatTime(r.detected_at, tz)}</td>
                    <td>
                      <strong>{r.carrier}</strong>
                      {r.label_text.length > 0 && (
                        <div className="small muted mono">
                          {r.label_text.slice(0, 2).join(" · ")}
                        </div>
                      )}
                    </td>
                    <td className="num big-num">{r.package_count}</td>
                    <td>
                      <StatusBadge status={r.status} />
                    </td>
                    <td className="reason">
                      {r.expected && (
                        <div>
                          <strong style={{ color: "var(--ink)" }}>{r.expected.supplier}</strong>
                          {r.expected.reference && (
                            <span className="muted"> · {r.expected.reference}</span>
                          )}
                        </div>
                      )}
                      {r.reason ?? "Reconciling…"}
                      {r.event_to_ledger_ms != null && (
                        <div className="small muted">
                          Logged {seconds(r.event_to_ledger_ms)} after the event
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card">
          <div className="card-head">
            <h2>Pipeline activity</h2>
            <span className="small muted">Last {a.events.length} camera events</span>
          </div>
          {a.events.length === 0 ? (
            <div className="empty">No camera events yet.</div>
          ) : (
            <ul className="activity">
              {a.events.map((x) => (
                <li key={x.id}>
                  <span className="when">{formatTime(x.received_at, tz, true)}</span>
                  <span>
                    <strong>{ORIGIN[x.origin]}</strong> · {eventText(x)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
