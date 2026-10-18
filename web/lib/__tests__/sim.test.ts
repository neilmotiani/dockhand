import { describe, expect, it } from "vitest";

import { showSimulated, simApiQuery, simPageQuery, toggleHref } from "../sim";

describe("simulated-data toggle", () => {
  it("is on unless sim=0", () => {
    expect(showSimulated(undefined)).toBe(true);
    expect(showSimulated("1")).toBe(true);
    expect(showSimulated("0")).toBe(false);
  });

  it("maps to the API and page parameters", () => {
    expect(simApiQuery(true)).toEqual({});
    expect(simApiQuery(false)).toEqual({ include_simulated: "false" });
    expect(simPageQuery(false)).toEqual({ sim: "0" });
  });

  it("flips the state and keeps the date", () => {
    expect(toggleHref("/", { date: "2026-10-10" }, true)).toBe("/?date=2026-10-10&sim=0");
    expect(toggleHref("/", { date: "2026-10-10", sim: "0" }, false)).toBe("/?date=2026-10-10");
    expect(toggleHref("/suppliers", {}, false)).toBe("/suppliers");
  });
});
