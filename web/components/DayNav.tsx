import Link from "next/link";

import { formatDay, shiftDay } from "@/lib/time";

/** Previous / next business day. `today` is the API's notion of today (business timezone). */
export function DayNav({ path, date, today }: { path: string; date: string; today: string }) {
  const href = (d: string) => (d === today ? path : `${path}?date=${d}`);
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
        <Link className="btn btn-sm" href={path}>
          Today
        </Link>
      )}
    </div>
  );
}
