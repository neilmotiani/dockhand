// Time helpers. Everything is shown in the business timezone the API reports, not the
// viewer's, so the ledger reads the same on any machine.

export function formatTime(iso: string, timeZone: string, seconds = false): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour: "numeric",
    minute: "2-digit",
    second: seconds ? "2-digit" : undefined,
  }).format(new Date(iso));
}

export function formatDay(date: string): string {
  // `date` is a calendar day (YYYY-MM-DD); format it at UTC noon so no offset can shift it.
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(`${date}T12:00:00Z`));
}

export function shiftDay(date: string, days: number): string {
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function offsetMs(utcMs: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(utcMs));
  const n = (t: string) => Number(parts.find((p) => p.type === t)?.value);
  const asUtc = Date.UTC(n("year"), n("month") - 1, n("day"), n("hour"), n("minute"), n("second"));
  return asUtc - utcMs;
}

/** Wall-clock `date` + `time` (HH:MM) in `timeZone` → ISO 8601 with that zone's UTC offset.
 *
 * The API rejects naive timestamps, and a browser's time input has no zone, so the form's
 * fields are interpreted in the business timezone. The zone's offsets 12 h either side of the
 * wall time are the only candidates (no zone changes offset twice in a day). A candidate is
 * valid when the instant it gives really shows that offset. Fall-back hour (two valid): the
 * earlier instant. Spring-forward gap (none valid): the pre-transition offset, so 02:30 becomes
 * 03:30. Same rules as Temporal's "compatible" disambiguation. */
export function zonedToIso(date: string, time: string, timeZone: string): string {
  const [y, mo, d] = date.split("-").map(Number);
  const [h, mi] = time.split(":").map(Number);
  if ([y, mo, d, h, mi].some((v) => !Number.isFinite(v))) {
    throw new Error(`invalid date or time: ${date} ${time}`);
  }
  const wall = Date.UTC(y, mo - 1, d, h, mi);
  const before = offsetMs(wall - 12 * 3600_000, timeZone);
  const after = offsetMs(wall + 12 * 3600_000, timeZone);
  const valid = [before, after]
    .map((o) => wall - o)
    .filter((utc) => wall - offsetMs(utc, timeZone) === utc);
  const utc = valid.length ? Math.min(...valid) : wall - before;
  const off = offsetMs(utc, timeZone);
  const sign = off < 0 ? "-" : "+";
  const abs = Math.abs(off) / 60000;
  const hh = String(Math.floor(abs / 60)).padStart(2, "0");
  const mm = String(abs % 60).padStart(2, "0");
  const local = new Date(utc + off).toISOString().slice(0, 19);
  return `${local}${sign}${hh}:${mm}`;
}

export function seconds(ms: number | null | undefined): string {
  return ms == null ? "–" : `${(ms / 1000).toFixed(1)} s`;
}
