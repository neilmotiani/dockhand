import Link from "next/link";

import { formatDay, shiftDay } from "@/lib/time";

/** Previous / next business day. `today` is the API's notion of today (business timezone).
 * `query` holds other parameters to keep while paging (e.g. the simulated-data toggle). */
export function DayNav({
  path,
  date,
  today,
  query = {},
}: {
  path: string;
  date: string;
  today: string;
  query?: Record<string, string>;
}) {
  const href = (d: string) => {
    const params = new URLSearchParams(d === today ? query : { date: d, ...query });
    const qs = params.toString();
    return qs ? `${path}?${qs}` : path;
  };
  return (
    <div className="daynav">
      <Link className="btn btn-sm" href={href(shiftDay(date, -1))} aria-label="Previous day">
        ←
      </Link>
      <span className="day">{date === today ? `Today · ${formatDay(date)}` : formatDay(date)}</span>
      <Link className="btn btn-sm" href={href(shiftDay(date, 1))} aria-label="Next day">
        →
      </Link>
      {date !== today && (
        <Link className="btn btn-sm" href={href(today)}>
          Today
        </Link>
      )}
    </div>
  );
}
