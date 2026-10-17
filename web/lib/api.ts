import "server-only";

import { connection } from "next/server";

// Server-side client for the Dockhand API. The admin token is read here and only here; this
// module cannot be imported into a client component ("server-only"), so the token never
// reaches the browser. Images go through app/media/[...path]/route.ts for the same reason.

const API_URL = (process.env.DOCKHAND_API_URL ?? "http://localhost:8010").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
  }
}

function headers(extra?: HeadersInit): Headers {
  const token = process.env.DOCKHAND_ADMIN_TOKEN;
  if (!token) {
    throw new ApiError("DOCKHAND_ADMIN_TOKEN is not set for the dashboard server.", null);
  }
  const h = new Headers(extra);
  h.set("Authorization", `Bearer ${token}`);
  return h;
}

async function detail(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join("; ");
    }
    return JSON.stringify(body);
  } catch {
    return res.statusText;
  }
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(`${API_URL}${path}`, {
      ...init,
      headers: headers(init?.headers),
      cache: "no-store",
    });
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(`Dockhand API unreachable at ${API_URL}`, null);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  // Ledger data is live: render at request time, never at build time.
  await connection();
  const res = await apiFetch(path);
  if (!res.ok) throw new ApiError(`GET ${path}: ${res.status} ${await detail(res)}`, res.status);
  return (await res.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await detail(res), res.status);
  return (await res.json()) as T;
}

/** `?date=...&...` from a page's searchParams. Anything but a YYYY-MM-DD date is dropped, so
 * the API falls back to today. */
export function withQuery(
  path: string,
  date: string | string[] | undefined,
  extra: Record<string, string> = {},
): string {
  const params = new URLSearchParams(extra);
  if (typeof date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(date)) params.set("date", date);
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}
