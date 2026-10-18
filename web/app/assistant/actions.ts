"use server";

import { ApiError, apiPost } from "@/lib/api";
import type { TurnRequest, TurnResponse } from "@/lib/assistant";

// One assistant turn, run on the dashboard server (which holds the admin token). The API runs
// the Bedrock tool loop and calls the MCP server; the browser only sees the result.

export type TurnOutcome = { ok: true; turn: TurnResponse } | { ok: false; message: string };

export async function askAssistant(req: TurnRequest): Promise<TurnOutcome> {
  try {
    return { ok: true, turn: await apiPost<TurnResponse>("/v1/assistant/turn", req) };
  } catch (e) {
    return { ok: false, message: e instanceof ApiError ? e.message : "Something went wrong." };
  }
}
