"use client";

/* eslint-disable @next/next/no-img-element -- card images are signed URLs on the MCP server,
   the same links a screen device would load; next/image would proxy them instead. */

import { useCallback, useEffect, useRef, useState } from "react";

import { askAssistant } from "@/app/assistant/actions";
import { SimulatedBadge } from "@/components/SimulatedBadge";
import { StatusBadge } from "@/components/StatusBadge";
import {
  type AssistantCard,
  type ConverseMessage,
  EXAMPLES,
  type PendingAction,
  type Recognition,
  type TraceEntry,
  type TurnRequest,
  type TurnResponse,
  confirmIntent,
  formatArgs,
  pickVoice,
  recognitionCtor,
  transcript,
} from "@/lib/assistant";

interface Entry {
  id: number;
  role: "user" | "assistant" | "error";
  text: string;
  cards?: AssistantCard[];
  pending?: PendingAction;
  /** What became of `pending`: confirmed, declined, or dropped by a new question. */
  resolved?: "yes" | "no" | "dropped";
  /** History before the request that led to `pending`, to drop it cleanly. */
  before?: ConverseMessage[];
}

interface TurnLog {
  id: number;
  label: string;
  trace: TraceEntry[];
  latency: TurnResponse["latency_ms"];
  usage: TurnResponse["usage"];
  model: string;
}

const SPEAK_KEY = "dockhand.assistant.speak";

function readSpeak(): boolean {
  try {
    return localStorage.getItem(SPEAK_KEY) !== "0";
  } catch {
    return true;
  }
}

export function Assistant() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [messages, setMessages] = useState<ConverseMessage[]>([]);
  const [log, setLog] = useState<TurnLog[]>([]);
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [voiceNote, setVoiceNote] = useState<string | null>(null);
  const [speak, setSpeak] = useState(true);
  const [canListen, setCanListen] = useState(false);
  const nextId = useRef(1);
  const recognition = useRef<Recognition | null>(null);
  const heard = useRef("");
  const partial = useRef("");
  // The recognizer's callbacks outlive the render that created them; they call the latest send.
  const sendRef = useRef<(q: string) => void>(() => {});
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Browser-only capabilities, read after mount so server and client render the same HTML.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSpeak(readSpeak());
    setCanListen(recognitionCtor(window) !== null);
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [entries, interim]);

  const say = useCallback(
    (reply: string) => {
      if (!speak || typeof window === "undefined" || !("speechSynthesis" in window)) return;
      const synth = window.speechSynthesis;
      synth.cancel();
      const u = new SpeechSynthesisUtterance(reply);
      const voice = pickVoice(synth.getVoices());
      if (voice) u.voice = voice as SpeechSynthesisVoice;
      u.rate = 1.03;
      synth.speak(u);
    },
    [speak],
  );

  const add = (e: Omit<Entry, "id">) => {
    const id = nextId.current++;
    setEntries((xs) => [...xs, { ...e, id }]);
    return id;
  };

  const run = async (req: TurnRequest, label: string) => {
    setBusy(true);
    const res = await askAssistant(req);
    setBusy(false);
    if (!res.ok) {
      add({ role: "error", text: res.message });
      return;
    }
    const t = res.turn;
    setMessages(t.messages);
    add({
      role: "assistant",
      text: t.reply,
      cards: t.cards,
      pending: t.pending ?? undefined,
      before: t.pending ? req.messages : undefined,
    });
    setLog((xs) => [
      {
        id: nextId.current++,
        label,
        trace: t.trace,
        latency: t.latency_ms,
        usage: t.usage,
        model: t.model_id,
      },
      ...xs,
    ]);
    say(t.reply);
  };

  const open = entries.findLast((e) => e.pending && !e.resolved);

  const answer = (entry: Entry, yes: boolean) => {
    if (!entry.pending || busy) return;
    setEntries((xs) =>
      xs.map((x) => (x.id === entry.id ? { ...x, resolved: yes ? "yes" : "no" } : x)),
    );
    add({ role: "user", text: yes ? "Yes, go ahead." : "No, cancel that." });
    void run(
      { messages, approval: { tool_use_id: entry.pending.tool_use_id, approved: yes } },
      yes ? "Confirmed" : "Declined",
    );
  };

  const send = (raw: string) => {
    const q = raw.trim();
    if (!q || busy) return;
    setText("");
    if (open) {
      const intent = confirmIntent(q);
      if (intent) {
        answer(open, intent === "yes");
        return;
      }
    }
    let history = messages;
    if (open) {
      // A new question instead of an answer: drop the unanswered request, because Converse
      // expects every tool use to be followed by its result.
      history = open.before ?? [];
      setEntries((xs) => xs.map((x) => (x.id === open.id ? { ...x, resolved: "dropped" } : x)));
    }
    add({ role: "user", text: q });
    void run({ messages: history, text: q }, q);
  };

  useEffect(() => {
    sendRef.current = send;
  });

  const startListening = () => {
    if (listening || busy) return;
    const Ctor = recognitionCtor(window);
    if (!Ctor) {
      setVoiceNote("Voice input needs Chrome, Edge or Safari. Type your question instead.");
      return;
    }
    window.speechSynthesis?.cancel();
    const r = new Ctor();
    r.lang = "en-US";
    r.interimResults = true;
    r.continuous = true;
    r.maxAlternatives = 1;
    heard.current = "";
    partial.current = "";
    r.onresult = (e) => {
      const t = transcript(e);
      heard.current = t.final;
      partial.current = t.interim;
      setInterim([t.final, t.interim].filter(Boolean).join(" "));
    };
    r.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        setVoiceNote("Microphone access was blocked. Allow it in the browser, or type instead.");
      } else if (e.error !== "no-speech" && e.error !== "aborted") {
        setVoiceNote(`Voice input failed (${e.error}). Type your question instead.`);
      }
    };
    r.onend = () => {
      setListening(false);
      setInterim("");
      recognition.current = null;
      // Released before the recognizer finalized: use what it had heard so far.
      const said = [heard.current, partial.current].filter(Boolean).join(" ");
      if (said) sendRef.current(said);
    };
    recognition.current = r;
    setVoiceNote(null);
    setListening(true);
    r.start();
  };

  const stopListening = () => recognition.current?.stop();

  const toggleSpeak = () => {
    const next = !speak;
    setSpeak(next);
    if (!next) window.speechSynthesis?.cancel();
    try {
      localStorage.setItem(SPEAK_KEY, next ? "1" : "0");
    } catch {
      // Private mode: the setting just isn't remembered.
    }
  };

  return (
    <div className="assistant">
      <section className="card assistant-chat" aria-label="Conversation">
        <div className="chat-log" aria-live="polite">
          {entries.length === 0 && (
            <div className="chat-empty">
              <p>
                Hold the microphone and ask about today’s deliveries, or type below. Try one of
                these:
              </p>
              <div className="chips">
                {EXAMPLES.map((q) => (
                  <button key={q} type="button" className="chip" onClick={() => send(q)}>
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}
          {entries.map((e) => (
            <div key={e.id} className={`bubble bubble-${e.role}`}>
              <p>{e.text}</p>
              {e.pending && (
                <div className="confirm" role="group" aria-label="Confirm action">
                  {e.resolved ? (
                    <span className="small muted">
                      {e.resolved === "yes"
                        ? "Confirmed"
                        : e.resolved === "no"
                          ? "Cancelled"
                          : "Skipped"}
                    </span>
                  ) : (
                    <>
                      <span className="small muted">Say “yes” or “no”, or</span>
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        disabled={busy}
                        onClick={() => answer(e, true)}
                      >
                        Confirm
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={busy}
                        onClick={() => answer(e, false)}
                      >
                        Cancel
                      </button>
                    </>
                  )}
                </div>
              )}
              {e.cards && e.cards.length > 0 && (
                <div className="answer-cards">
                  {e.cards.map((c, i) => (
                    <AnswerCard key={`${c.title}-${i}`} card={c} />
                  ))}
                </div>
              )}
            </div>
          ))}
          {listening && (
            <div className="bubble bubble-user bubble-interim">
              <p>{interim || "Listening…"}</p>
            </div>
          )}
          {busy && (
            <div className="bubble bubble-assistant bubble-thinking" aria-label="Working">
              <span className="dots" aria-hidden>
                <span />
                <span />
                <span />
              </span>
            </div>
          )}
          <div ref={bottom} />
        </div>

        <form
          className="chat-input"
          onSubmit={(e) => {
            e.preventDefault();
            send(text);
          }}
        >
          <button
            type="button"
            className={`mic${listening ? " mic-on" : ""}`}
            aria-pressed={listening}
            aria-label={listening ? "Release to send" : "Hold to talk"}
            title={canListen ? "Hold to talk (or hold Space)" : "Voice input isn't available here"}
            disabled={busy}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
              startListening();
            }}
            onPointerUp={stopListening}
            onPointerCancel={stopListening}
            onKeyDown={(e) => {
              if ((e.key === " " || e.key === "Enter") && !e.repeat) {
                e.preventDefault();
                startListening();
              }
            }}
            onKeyUp={(e) => {
              if (e.key === " " || e.key === "Enter") stopListening();
            }}
          >
            <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden>
              <path
                fill="currentColor"
                d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-2.08A7 7 0 0 0 19 12h-2Z"
              />
            </svg>
          </button>
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={open ? "Say yes or no, or ask something else" : "Ask about deliveries…"}
            aria-label="Message"
            maxLength={1000}
            disabled={busy}
          />
          <button type="submit" className="btn btn-primary" disabled={busy || !text.trim()}>
            Send
          </button>
          <button
            type="button"
            className="btn"
            onClick={toggleSpeak}
            aria-pressed={speak}
            title={speak ? "Spoken replies on" : "Spoken replies off"}
          >
            {speak ? "🔊" : "🔇"}
            <span className="sr-only">Spoken replies</span>
          </button>
        </form>
        {voiceNote && (
          <div className="notice notice-err voice-note" role="status">
            {voiceNote}
          </div>
        )}
      </section>

      <McpPanel log={log} />
    </div>
  );
}

function AnswerCard({ card }: { card: AssistantCard }) {
  return (
    <article className="answer-card">
      {card.image_url ? (
        <a href={card.image_url} target="_blank" rel="noreferrer" className="answer-img">
          <img src={card.image_url} alt={`Snapshot: ${card.title}`} loading="lazy" />
        </a>
      ) : null}
      <div className="answer-body">
        <div className="answer-badges">
          {card.status && <StatusBadge status={card.status} />}
          {card.simulated && <SimulatedBadge />}
        </div>
        <h3>{card.title}</h3>
        {card.subtitle && <p className="small">{card.subtitle}</p>}
        {card.facts.length > 0 && (
          <dl className="answer-facts">
            {card.facts.map((f) => (
              <div key={f.label}>
                <dt>{f.label}</dt>
                <dd>{f.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    </article>
  );
}

function McpPanel({ log }: { log: TurnLog[] }) {
  const calls = log.reduce((n, t) => n + t.trace.length, 0);
  return (
    <details className="card mcp-panel" open>
      <summary>
        <span>MCP tool calls</span>
        <span className="small muted">{calls}</span>
      </summary>
      <p className="small muted mcp-about">
        Each turn, the assistant (Bedrock Converse, tool use) connects to the Dockhand MCP server
        over Streamable HTTP, negotiates protocol 2025-11-25, lists the tools and calls them. Newest
        first.
      </p>
      {log.length === 0 && <p className="small muted mcp-about">No calls yet.</p>}
      <ol className="mcp-turns">
        {log.map((t) => (
          <li key={t.id}>
            <div className="mcp-turn-head">
              <span className="mcp-q">{t.label}</span>
              <span className="small muted">
                {Math.round(t.latency.total_ms)} ms · {t.usage.input_tokens + t.usage.output_tokens}{" "}
                tokens · {t.model}
              </span>
            </div>
            <ul className="mcp-calls">
              {t.trace.map((c, i) => (
                <TraceRow key={i} entry={c} />
              ))}
            </ul>
          </li>
        ))}
      </ol>
    </details>
  );
}

function TraceRow({ entry: c }: { entry: TraceEntry }) {
  const state = c.declined ? "declined" : c.is_error ? "error" : "ok";
  if (c.method === "tools/list") {
    return (
      <li className="mcp-call">
        <code>tools/list</code> <span className="small muted">→ {c.tools?.length ?? 0} tools</span>
        {c.duration_ms != null && <span className="mcp-ms">{Math.round(c.duration_ms)} ms</span>}
      </li>
    );
  }
  return (
    <li className={`mcp-call mcp-${state}`}>
      <div>
        <code>tools/call</code> <strong>{c.tool}</strong>
        <span className="mcp-args">({formatArgs(c.arguments)})</span>
        {c.duration_ms != null && <span className="mcp-ms">{Math.round(c.duration_ms)} ms</span>}
      </div>
      <div className="small mcp-result">
        {c.declined
          ? "Not run: the user declined."
          : c.is_error
            ? `isError: ${c.error ?? "tool error"}`
            : c.summary}
      </div>
      {c.result && (
        <details className="mcp-raw">
          <summary className="small">structuredContent · {c.content_types?.join(", ")}</summary>
          <pre>{JSON.stringify(c.result, null, 2)}</pre>
        </details>
      )}
    </li>
  );
}
