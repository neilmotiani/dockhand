import { ApiProblem } from "@/components/ApiProblem";
import { apiGet } from "@/lib/api";
import { seconds } from "@/lib/time";
import type { Metrics, Ratio } from "@/lib/types";

// Every number on this page comes from GET /v1/metrics, which copies it, with its N, from the
// committed results files (eval/results, bench/results). Nothing is computed or rounded up
// here beyond formatting a ratio to one decimal place.

function pct(r: Ratio | null): string {
  return r ? `${Number((r.value * 100).toFixed(1))}%` : "n/a";
}

function frac(r: Ratio | null): string {
  return r ? `${r.k}/${r.n}` : "";
}

function Cite({ file, commit, at }: { file: string; commit: string; at: string }) {
  return (
    <div className="cite">
      Source: <code>{file}</code> · commit <code>{commit.slice(0, 7)}</code> · {at.slice(0, 10)}
    </div>
  );
}

const FLOW = [
  { title: "Ring camera", body: "Event History poll, signed webhook, or the demo trigger" },
  { title: "Ingest", body: "HMAC check, dedupe on request ID, durable Postgres queue" },
  { title: "Capture", body: "WHEP live view; stop once the scene settles; best 3 of 10 frames" },
  { title: "Extract", body: "Amazon Bedrock (Nova Pro): carrier, box count, label text" },
  { title: "Reconcile", body: "Optimal assignment against expected orders; status + reason" },
  { title: "Ledger", body: "Postgres, with evidence frames; this dashboard reads it" },
];

function Diagram() {
  return (
    <div className="flow" role="img" aria-label={FLOW.map((f) => f.title).join(" → ")}>
      {FLOW.map((f, i) => (
        <div key={f.title} className="flow-step">
          <div className="flow-node">
            <div className="flow-num">{i + 1}</div>
            <h3>{f.title}</h3>
            <p>{f.body}</p>
          </div>
          {i < FLOW.length - 1 && (
            <div className="flow-arrow" aria-hidden>
              →
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export default async function HowItWorksPage() {
  let m: Metrics;
  try {
    m = await apiGet<Metrics>("/v1/metrics");
  } catch (err) {
    return <ApiProblem error={err} />;
  }
  const latest = m.latency.at(-1);
  const first = m.latency.length > 1 ? m.latency[0] : undefined;
  const ext = m.extraction;
  const rec = m.reconciliation;
  const headline = ext?.rows.find(
    (r) => r.source === "roboflow_porch_piracy" && r.model === "us.amazon.nova-pro-v1:0",
  );

  return (
    <>
      <div className="page-head">
        <div>
          <h1>How it works</h1>
          <p>From a Ring event to a reconciled ledger entry, and how well each step does.</p>
        </div>
      </div>

      <div className="stack">
        <section className="card card-pad">
          <Diagram />
        </section>

        <div className="metric-grid">
          <section className="card card-pad">
            <div className="tile-label">
              Event → ledger, {latest?.label.toLowerCase() ?? "latency"}
            </div>
            {latest?.event_to_ledger ? (
              <>
                <div className="metric-value">
                  {seconds(latest.event_to_ledger.p50_ms)}
                  <span className="metric-n">N={latest.event_to_ledger.n}</span>
                </div>
                <div className="small">
                  p50 · p95 {seconds(latest.event_to_ledger.p95_ms)}
                  {first?.event_to_ledger &&
                    ` · was ${seconds(first.event_to_ledger.p50_ms)} p50 with ${first.label.toLowerCase()} (N=${first.event_to_ledger.n})`}
                </div>
                <div className="small muted">
                  Live Ring Playground runs. N is small: individual runs, not a distribution.
                </div>
                <Cite file={latest.file} commit={latest.git_commit} at={latest.generated_at} />
              </>
            ) : (
              <div className="muted">No latency results committed yet.</div>
            )}
          </section>

          <section className="card card-pad">
            <div className="tile-label">Box count exact match, real doorbell footage</div>
            {headline && ext ? (
              <>
                <div className="metric-value">
                  {pct(headline.count_exact)}
                  <span className="metric-n">N={headline.n}</span>
                </div>
                <div className="small">
                  {frac(headline.count_exact)} correct · {headline.model_label} ·{" "}
                  {headline.source_label}
                </div>
                <div className="small muted">
                  Every source is reported separately below; they are never pooled.
                </div>
                <Cite file={ext.file} commit={ext.git_commit} at={ext.generated_at} />
              </>
            ) : (
              <div className="muted">No extraction results committed yet.</div>
            )}
          </section>

          <section className="card card-pad">
            <div className="tile-label">
              Discrepancy detection (short / over / missing / unexpected)
            </div>
            {rec ? (
              <>
                <div className="metric-value">
                  {pct(rec.precision)}{" "}
                  <span style={{ fontSize: 18, fontWeight: 550 }}>precision</span>
                </div>
                <div className="small">
                  {frac(rec.precision)} flagged were right · recall {pct(rec.recall)} (
                  {frac(rec.recall)})
                </div>
                <div className="small muted">
                  Synthetic: {rec.n_days} simulated days, {rec.n_deliveries} deliveries. Not Ring
                  footage.
                </div>
                <Cite file={rec.file} commit={rec.git_commit} at={rec.generated_at} />
              </>
            ) : (
              <div className="muted">No reconciliation results committed yet.</div>
            )}
          </section>
        </div>

        {ext && (
          <section className="card">
            <div className="card-head">
              <h2>Extraction accuracy by source</h2>
              <span className="small muted">Prompt {ext.prompt_version}</span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Model</th>
                  <th className="num">N</th>
                  <th className="num">Box count exact</th>
                  <th className="num">Carrier</th>
                  <th className="num">False alarms on empty scenes</th>
                  <th className="num">p50 latency</th>
                  <th className="num">$/call</th>
                </tr>
              </thead>
              <tbody>
                {ext.rows.map((r) => (
                  <tr key={`${r.source}-${r.model}`}>
                    <td>{r.source_label}</td>
                    <td className="nowrap">{r.model_label}</td>
                    <td className="num">{r.n}</td>
                    <td className="num">
                      {pct(r.count_exact)}{" "}
                      <span className="muted small">({frac(r.count_exact)})</span>
                    </td>
                    <td className="num">
                      {r.carrier_accuracy ? (
                        <>
                          {pct(r.carrier_accuracy)}{" "}
                          <span className="muted small">({frac(r.carrier_accuracy)})</span>
                        </>
                      ) : (
                        <span className="muted">not labelled</span>
                      )}
                    </td>
                    <td className="num">
                      {r.false_alarm ? (
                        <>
                          {pct(r.false_alarm)}{" "}
                          <span className="muted small">({frac(r.false_alarm)})</span>
                        </>
                      ) : (
                        <span className="muted">n/a</span>
                      )}
                    </td>
                    <td className="num">{seconds(r.latency_ms_p50)}</td>
                    <td className="num">${r.usd_per_call.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="card-pad" style={{ paddingTop: 4 }}>
              <ul className="caveats">
                {ext.caveats.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
              <Cite file={ext.file} commit={ext.git_commit} at={ext.generated_at} />
            </div>
          </section>
        )}

        {m.latency.length > 0 && (
          <section className="card">
            <div className="card-head">
              <h2>Latency by pipeline stage</h2>
              <span className="small muted">
                Live Ring Playground runs, nearest-rank percentiles
              </span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Capture rule</th>
                  <th className="num">N</th>
                  <th className="num">Event → ledger p50 / p95</th>
                  <th className="num">WHEP setup p50</th>
                  <th className="num">Capture p50</th>
                  <th className="num">Bedrock extract p50</th>
                  <th className="num">Reconcile p50</th>
                </tr>
              </thead>
              <tbody>
                {m.latency.map((l) => (
                  <tr key={l.file}>
                    <td>
                      {l.label}
                      <div className="cite" style={{ marginTop: 2 }}>
                        <code>{l.file}</code>
                      </div>
                    </td>
                    <td className="num">{l.n_deliveries}</td>
                    <td className="num">
                      {seconds(l.event_to_ledger?.p50_ms)} / {seconds(l.event_to_ledger?.p95_ms)}
                    </td>
                    <td className="num">{seconds(l.stages.whep_setup?.p50_ms)}</td>
                    <td className="num">{seconds(l.stages.capture?.p50_ms)}</td>
                    <td className="num">{seconds(l.stages.extract?.p50_ms)}</td>
                    <td className="num">
                      {l.stages.reconcile ? `${l.stages.reconcile.p50_ms} ms` : "–"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
      </div>
    </>
  );
}
