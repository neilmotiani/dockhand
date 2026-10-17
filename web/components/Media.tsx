"use client";

/* eslint-disable @next/next/no-img-element -- evidence images come through our own
   authenticated proxy (app/media), which next/image's optimizer would bypass. */

import { useEffect, useRef, useState } from "react";

import { mediaSrc } from "@/lib/media";

/** A thumbnail that opens the full image in a modal. `gallery` (API paths, including `src`)
 * lets the modal step through all evidence frames with ← / →. Esc, the close button or a
 * click outside the image closes it (native <dialog>, so focus and Esc come for free). */
function Zoomable({
  src,
  alt,
  caption,
  gallery,
  className,
  lazy = true,
}: {
  src: string;
  alt: string;
  caption?: string;
  gallery?: string[];
  className?: string;
  lazy?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const images = gallery?.includes(src) ? gallery : [src, ...(gallery ?? [])];
  const [index, setIndex] = useState(Math.max(0, images.indexOf(src)));
  const step = (by: number) => setIndex((i) => (i + by + images.length) % images.length);

  useEffect(() => {
    const d = dialog.current;
    if (!d) return;
    const onKey = (e: KeyboardEvent) => {
      if (images.length < 2) return;
      if (e.key === "ArrowRight") step(1);
      if (e.key === "ArrowLeft") step(-1);
    };
    d.addEventListener("keydown", onKey);
    return () => d.removeEventListener("keydown", onKey);
  });

  return (
    <>
      <button
        type="button"
        className="zoom-trigger"
        aria-label={`Enlarge: ${alt}`}
        onClick={() => {
          setIndex(Math.max(0, images.indexOf(src)));
          dialog.current?.showModal();
        }}
      >
        <img
          className={className}
          src={mediaSrc(src)}
          alt={alt}
          loading={lazy ? "lazy" : undefined}
        />
        <span className="zoom-hint" aria-hidden>
          ⤢
        </span>
      </button>
      <dialog
        ref={dialog}
        className="lightbox"
        aria-label={alt}
        onClick={(e) => {
          // A click on the backdrop lands on the <dialog> element itself.
          if (e.target === e.currentTarget) dialog.current?.close();
        }}
      >
        <figure>
          <img src={mediaSrc(images[index])} alt={alt} />
          <figcaption>
            <span>
              {caption}
              {images.length > 1 && ` · frame ${index + 1} of ${images.length}`}
            </span>
            <span className="lightbox-actions">
              {images.length > 1 && (
                <>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => step(-1)}
                    aria-label="Previous frame"
                  >
                    ←
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => step(1)}
                    aria-label="Next frame"
                  >
                    →
                  </button>
                </>
              )}
              <a
                className="btn btn-sm"
                href={mediaSrc(images[index])}
                target="_blank"
                rel="noreferrer"
              >
                Open original
              </a>
              <button type="button" className="btn btn-sm" onClick={() => dialog.current?.close()}>
                Close
              </button>
            </span>
          </figcaption>
        </figure>
      </dialog>
    </>
  );
}

export function Thumb({
  src,
  alt,
  caption,
  gallery,
}: {
  src: string | null;
  alt: string;
  caption?: string;
  gallery?: string[];
}) {
  if (!src) return <div className="thumb-empty">No image</div>;
  return <Zoomable src={src} alt={alt} caption={caption} gallery={gallery} className="thumb" />;
}

export function EvidenceImage({
  src,
  alt,
  caption,
  gallery,
}: {
  src: string;
  alt: string;
  caption?: string;
  gallery?: string[];
}) {
  return (
    <Zoomable
      src={src}
      alt={alt}
      caption={caption}
      gallery={gallery}
      className="evidence-main"
      lazy={false}
    />
  );
}

export function Frames({
  frames,
  alt,
  caption,
  gallery,
}: {
  frames: string[];
  alt: string;
  caption?: string;
  gallery?: string[];
}) {
  if (frames.length === 0) return null;
  return (
    <div className="frames">
      {frames.map((f, i) => (
        <Zoomable
          key={f}
          src={f}
          alt={`${alt}, frame ${i + 1}`}
          caption={caption}
          gallery={gallery}
        />
      ))}
    </div>
  );
}
