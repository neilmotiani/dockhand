// The simulated Alexa+ assistant: response shapes of POST /v1/assistant/turn
// (app/assistant/agent.py) and the browser-side helpers the page uses. Shared by server and
// client components.

import type { Status } from "./types";

export interface CardFact {
  label: string;
  value: string;
}

/** A screen card from an MCP tool result (mcp_server/tools.py `Card`). */
export interface AssistantCard {
  title: string;
  subtitle: string | null;
  status: Status | null;
  /** Signed snapshot URL served by the MCP server; expires (MCP_MEDIA_TTL_S). */
  image_url: string | null;
  simulated: boolean;
  facts: CardFact[];
}

export interface TraceEntry {
  method: "tools/list" | "tools/call";
  tool?: string;
  tools?: string[];
  arguments?: Record<string, unknown>;
  is_error?: boolean;
  declined?: boolean;
  duration_ms?: number;
  summary?: string | null;
  error?: string | null;
  result?: Record<string, unknown> | null;
  content_types?: string[];
}

export interface PendingAction {
  tool_use_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  question: string;
}

/** Bedrock Converse messages; opaque to the page, sent back on the next turn. */
export type ConverseMessage = { role: "user" | "assistant"; content: unknown[] };

export interface TurnResponse {
  reply: string;
  messages: ConverseMessage[];
  cards: AssistantCard[];
  trace: TraceEntry[];
  pending: PendingAction | null;
  latency_ms: {
    total_ms: number;
    tools_list_ms?: number;
    model_ms: number[];
    tool_ms: number[];
  };
  usage: { input_tokens: number; output_tokens: number };
  model_id: string;
}

export type TurnRequest =
  | { messages: ConverseMessage[]; text: string }
  | { messages: ConverseMessage[]; approval: { tool_use_id: string; approved: boolean } };

export const EXAMPLES = [
  "Did the Sysco delivery come today?",
  "Which supplier shorts us the most?",
  "Add an expected delivery from Daybreak Bakery, 4 boxes, tomorrow 7 to 9 AM",
];

/** A spoken answer to a confirmation question, or null if it's a new request. */
export function confirmIntent(text: string): "yes" | "no" | null {
  const t = text
    .trim()
    .toLowerCase()
    .replace(/[.!,?]+$/g, "");
  if (/^(yes|yeah|yep|yup|sure|ok|okay|confirm|do it|go ahead|please do|correct)\b/.test(t)) {
    return "yes";
  }
  if (/^(no|nope|cancel|stop|don't|do not|never mind|nevermind)\b/.test(t)) return "no";
  return null;
}

/** `supplier: "Sysco", date: "today"` for the MCP activity panel. */
export function formatArgs(args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return "{}";
  return Object.entries(args)
    .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
    .join(", ");
}

// Web Speech API. Chrome and Safari still expose recognition under the webkit prefix only,
// and TypeScript's DOM types don't include it, so it is typed here.
export interface Recognition {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: RecognitionEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
}

export interface RecognitionEvent {
  resultIndex: number;
  results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>;
}

type RecognitionCtor = new () => Recognition;

export function recognitionCtor(w: object): RecognitionCtor | null {
  const g = w as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return g.SpeechRecognition ?? g.webkitSpeechRecognition ?? null;
}

/** Final and interim text from a recognition event, joined over every result so far. */
export function transcript(e: RecognitionEvent): { final: string; interim: string } {
  let final = "";
  let interim = "";
  for (let i = 0; i < e.results.length; i++) {
    const r = e.results[i];
    if (r.isFinal) final += r[0].transcript;
    else interim += r[0].transcript;
  }
  return { final: final.trim(), interim: interim.trim() };
}

/** An English voice for the reply, preferring local, natural-sounding ones. */
export function pickVoice(voices: { name: string; lang: string; localService: boolean }[]) {
  const en = voices.filter((v) => v.lang.toLowerCase().startsWith("en"));
  const us = en.filter((v) => v.lang.toLowerCase() === "en-us");
  const pool = us.length ? us : en;
  return (
    pool.find((v) => /natural|samantha|google us english|aria|jenny/i.test(v.name)) ??
    pool.find((v) => v.localService) ??
    pool[0] ??
    null
  );
}
