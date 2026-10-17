/* eslint-disable @next/next/no-img-element -- evidence images come through our own
   authenticated proxy (app/media), which next/image's optimizer would bypass. */

/** API media path (/v1/deliveries/...) → the dashboard's proxy route (/media/deliveries/...). */
export function mediaSrc(apiPath: string): string {
  return apiPath.replace(/^\/v1\//, "/media/");
}

export function Thumb({ src, alt }: { src: string | null; alt: string }) {
  if (!src) return <div className="thumb-empty">No image</div>;
  return <img className="thumb" src={mediaSrc(src)} alt={alt} loading="lazy" />;
}

export function EvidenceImage({ src, alt }: { src: string; alt: string }) {
  return (
    <a href={mediaSrc(src)} target="_blank" rel="noreferrer">
      <img className="evidence-main" src={mediaSrc(src)} alt={alt} />
    </a>
  );
}

export function Frames({ frames, alt }: { frames: string[]; alt: string }) {
  if (frames.length < 2) return null;
  return (
    <div className="frames">
      {frames.map((f, i) => (
        <a key={f} href={mediaSrc(f)} target="_blank" rel="noreferrer">
          <img src={mediaSrc(f)} alt={`${alt}, frame ${i + 1}`} loading="lazy" />
        </a>
      ))}
    </div>
  );
}
