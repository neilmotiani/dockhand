import { describe, expect, it } from "vitest";

import { confirmIntent, formatArgs, pickVoice, recognitionCtor, transcript } from "../assistant";

describe("confirmation by voice", () => {
  it("hears yes and no", () => {
    for (const t of ["Yes", "yes.", "Yeah go ahead", "OK", "confirm", "do it!"]) {
      expect(confirmIntent(t)).toBe("yes");
    }
    for (const t of ["No", "no thanks", "Cancel", "don't", "never mind"]) {
      expect(confirmIntent(t)).toBe("no");
    }
  });

  it("treats anything else as a new request", () => {
    expect(confirmIntent("Did the Sysco delivery come today?")).toBeNull();
    expect(confirmIntent("nobody delivered?")).toBeNull();
    expect(confirmIntent("yesterday's deliveries")).toBeNull();
  });
});

describe("MCP activity panel", () => {
  it("formats tool arguments", () => {
    expect(formatArgs({ supplier: "Sysco", date: "today" })).toBe(
      'supplier: "Sysco", date: "today"',
    );
    expect(formatArgs({ count: 4 })).toBe("count: 4");
    expect(formatArgs({})).toBe("{}");
    expect(formatArgs(undefined)).toBe("{}");
  });
});

describe("Web Speech API", () => {
  it("finds the recognizer, prefixed or not", () => {
    class R {}
    expect(recognitionCtor({ SpeechRecognition: R })).toBe(R);
    expect(recognitionCtor({ webkitSpeechRecognition: R })).toBe(R);
    expect(recognitionCtor({})).toBeNull();
  });

  it("joins final and interim results", () => {
    const result = (t: string, isFinal: boolean) => Object.assign([{ transcript: t }], { isFinal });
    const e = {
      resultIndex: 0,
      results: [result("did the Sysco ", true), result("delivery come", false)],
    };
    expect(transcript(e)).toEqual({ final: "did the Sysco", interim: "delivery come" });
  });

  it("picks an English voice, preferring natural ones", () => {
    const v = (name: string, lang: string, localService = true) => ({ name, lang, localService });
    expect(
      pickVoice([v("Thomas", "fr-FR"), v("Fred", "en-US"), v("Samantha", "en-US")])?.name,
    ).toBe("Samantha");
    expect(pickVoice([v("Daniel", "en-GB")])?.name).toBe("Daniel");
    expect(pickVoice([v("Thomas", "fr-FR")])).toBeNull();
  });
});
