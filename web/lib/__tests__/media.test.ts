import { describe, expect, it } from "vitest";

import { evidence, mediaSrc } from "../media";

const frames = [
  "/v1/deliveries/x/frames/0",
  "/v1/deliveries/x/frames/1",
  "/v1/deliveries/x/frames/2",
];

describe("evidence", () => {
  it("uses the snapshot's own frame as the main image and lists the rest", () => {
    const e = evidence({ snapshot: "/v1/deliveries/x/snapshot", snapshot_frame: 2, frames });
    expect(e.main).toBe(frames[2]);
    expect(e.others).toEqual([frames[0], frames[1]]);
  });

  it("falls back to the snapshot URL when its rank is unknown or out of range", () => {
    for (const k of [null, 5]) {
      const e = evidence({ snapshot: "/v1/deliveries/x/snapshot", snapshot_frame: k, frames });
      expect(e.main).toBe("/v1/deliveries/x/snapshot");
      expect(e.others).toEqual(frames);
    }
  });

  it("maps API paths to the dashboard's proxy", () => {
    expect(mediaSrc("/v1/deliveries/x/frames/1")).toBe("/media/deliveries/x/frames/1");
  });
});
