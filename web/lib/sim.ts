// The "Show simulated data" toggle. It lives in the URL (`?sim=0` hides simulated history), so
// it survives a reload, works without client state, and the server page can pass it to the API.
// On by default: the dashboard opens with the demo history visible, every row badged.

export const SIM_PARAM = "sim";

export function showSimulated(value: string | string[] | undefined): boolean {
  return value !== "0";
}

/** API query parameters for the toggle state. */
export function simApiQuery(show: boolean): Record<string, string> {
  return show ? {} : { include_simulated: "false" };
}

/** Page query parameters to keep the toggle state across links (day navigation). */
export function simPageQuery(show: boolean): Record<string, string> {
  return show ? {} : { [SIM_PARAM]: "0" };
}

/** The current page's URL with the toggle flipped, other parameters kept. */
export function toggleHref(path: string, params: Record<string, string>, show: boolean): string {
  const q = new URLSearchParams(params);
  if (show) q.set(SIM_PARAM, "0");
  else q.delete(SIM_PARAM);
  const qs = q.toString();
  return qs ? `${path}?${qs}` : path;
}
