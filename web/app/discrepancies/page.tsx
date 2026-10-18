import { ApiProblem } from "@/components/ApiProblem";
import { AutoRefresh } from "@/components/AutoRefresh";
import { CopyButton } from "@/components/CopyButton";
import { DayNav } from "@/components/DayNav";
import { carrierVisible, NOT_VISIBLE_HELP } from "@/components/Carrier";
import { EvidenceImage, Frames } from "@/components/Media";
import { evidence, mediaSrc } from "@/lib/media";
import { SimToggle } from "@/components/SimToggle";
import { SimulatedBadge } from "@/components/SimulatedBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { apiGet, withQuery } from "@/lib/api";
import { showSimulated, simApiQuery, simPageQuery, toggleHref } from "@/lib/sim";
import { disputeSummary } from "@/lib/dispute";
import { formatTime } from "@/lib/time";
import type { Day, Discrepancy } from "@/lib/types";

function title(x: Discrepancy): string {
  const e = x.expected;
  const d = x.delivery;
  const who = e ? `${e.supplier}${e.reference ? ` · ${e.reference}` : ""}` : "";
  switch (x.status) {
    case "short":
      return `${who}: short by ${e!.expected_count - d!.package_count}`;
    case "over":
      return `${who}: over by ${d!.package_count - e!.expected_count}`;
    case "missing":
      return `${who}: never arrived`;
    case "unexpected":
      return carrierVisible(d!.carrier)
        ? `${d!.carrier} delivery, no matching order`
        : "Delivery with no matching order";
  }
}

function Card({ x, tz, business }: { x: Discrepancy; tz: string; business?: string }) {
  const d = x.delivery;
  const e = x.expected;
  const summary = disputeSummary(x, tz, business);
  return (
    <article className="card disc">
      <div className="disc-body">
        <div className="disc-title">
          <StatusBadge status={x.status} large />
          <h2>{title(x)}</h2>
          {x.simulated && <SimulatedBadge />}
        </div>
        <dl className="disc-facts">
          {e && (
            <>
              <dt>Expected</dt>
              <dd>
                {e.expected_count} box{e.expected_count === 1 ? "" : "es"}
                {e.carrier ? ` via ${e.carrier}` : ""}, {formatTime(e.window_start, tz)} –{" "}
                {formatTime(e.window_end, tz)}
              </dd>
            </>
          )}
          <dt>Camera saw</dt>
          <dd>
            {d ? (
              <>
                {d.package_count} box{d.package_count === 1 ? "" : "es"} at{" "}
                {formatTime(d.detected_at, tz)},{" "}
                {carrierVisible(d.carrier) ? (
                  `carrier ${d.carrier}`
                ) : (
                  <span title={NOT_VISIBLE_HELP}>carrier not visible</span>
                )}
              </>
            ) : (
              "Nothing matching during the window"
            )}
          </dd>
          <dt>Why</dt>
          <dd style={{ fontWeight: 400 }}>{x.reason}</dd>
        </dl>
        <pre className="dispute-preview" aria-label="Dispute summary">
          {summary}
        </pre>
        <div>
          <CopyButton text={summary} label="Copy dispute summary" />
        </div>
      </div>
      <aside className="disc-evidence">
        <h3>Evidence</h3>
        {d?.media.clip ? (
          <video
            className="evidence-main"
            src={mediaSrc(d.media.clip)}
            controls
            preload="metadata"
            poster={d.media.snapshot ? mediaSrc(d.media.snapshot) : undefined}
          />
        ) : d && evidence(d.media).main ? (
          <EvidenceImage
            src={evidence(d.media).main!}
            gallery={d.media.frames}
            alt={`${d.simulated ? "Synthetic composite for a simulated delivery" : "Ring snapshot"} at ${formatTime(d.detected_at, tz)}`}
            caption={
              d.simulated
                ? `Simulated delivery, ${formatTime(d.detected_at, tz)} (synthetic composite)`
                : `Ring camera, ${formatTime(d.detected_at, tz)}`
            }
          />
        ) : (
          <div className="evidence-none">
            {d
              ? "No snapshot was saved for this delivery."
              : "No delivery was recorded, so there is no footage."}
          </div>
        )}
        {d && (
          <Frames
            frames={evidence(d.media).others}
            gallery={d.media.frames}
            alt="Other frames read by the model"
            caption={`Ring camera, ${formatTime(d.detected_at, tz)}`}
          />
        )}
        {d && d.simulated && (
          <div className="small muted">
            Simulated demo history · illustrative synthetic composite, not camera footage
          </div>
        )}
        {d && !d.simulated && (
          <div className="small muted">
            Ring camera ·{" "}
            {d.source === "ring_playground" ? "Ring Developer Playground" : "Ring device"}
            {d.frames_sent
              ? ` · ${d.frames_sent} of ${d.frames_sampled} sampled frames sent to the model`
              : ""}
          </div>
        )}
      </aside>
    </article>
  );
}

export default async function DiscrepanciesPage({ searchParams }: PageProps<"/discrepancies">) {
  const { date, sim } = await searchParams;
  const show = showSimulated(sim);
  let data;
  try {
    data = await apiGet<Day & { discrepancies: Discrepancy[] }>(
      withQuery("/v1/discrepancies", date, simApiQuery(show)),
    );
  } catch (err) {
    return <ApiProblem error={err} />;
  }
  const business = process.env.DOCKHAND_BUSINESS_NAME || undefined;
  return (
    <>
      {data.date === data.today && <AutoRefresh ms={5000} />}
      <div className="page-head">
        <div>
          <h1>Discrepancies</h1>
          <p>Short, missing and unexpected deliveries, with the camera evidence to back them up.</p>
        </div>
        <div className="head-tools">
          <DayNav
            path="/discrepancies"
            date={data.date}
            today={data.today}
            query={simPageQuery(show)}
          />
          <SimToggle
            show={show}
            href={toggleHref(
              "/discrepancies",
              { ...(data.date === data.today ? {} : { date: data.date }), ...simPageQuery(show) },
              show,
            )}
          />
        </div>
      </div>
      {data.discrepancies.length === 0 ? (
        <div className="card empty">
          <StatusBadge status="matched" label="All clear" /> Nothing to dispute on this day.
        </div>
      ) : (
        <div className="stack">
          {data.discrepancies.map((x) => (
            <Card key={x.id} x={x} tz={data.timezone} business={business} />
          ))}
        </div>
      )}
    </>
  );
}
