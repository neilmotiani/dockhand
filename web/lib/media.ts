// Evidence image helpers, shared by server and client components.

/** API media path (/v1/deliveries/...) → the dashboard's proxy route (/media/deliveries/...). */
export function mediaSrc(apiPath: string): string {
  return apiPath.replace(/^\/v1\//, "/media/");
}

/** The snapshot as one of the frames (so the modal can step through all of them), plus the
 * other frames. Falls back to the plain snapshot URL when the rank isn't known. */
export function evidence(media: {
  snapshot: string | null;
  snapshot_frame: number | null;
  frames: string[];
}): { main: string | null; others: string[] } {
  const k = media.snapshot_frame;
  if (k != null && k >= 0 && k < media.frames.length) {
    return { main: media.frames[k], others: media.frames.filter((_, i) => i !== k) };
  }
  return { main: media.snapshot, others: media.frames };
}
