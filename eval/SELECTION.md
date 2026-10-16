# Extraction eval set: selection and labelling rules

Written and committed on 2026-10-16, before any model was run on any item in this set. Any later
change to these rules gets its own commit, with the reason, before the eval is re-run.

The set has four sources. Each source is scored and reported on its own row with its own N, and is
never pooled with another source in a headline number. Every item carries `source` and
`provenance` in `eval/labels/items.jsonl`.

| Source | `source` | `provenance` | Real or synthetic |
|---|---|---|---|
| Porch Piracy (Roboflow Universe, CC BY 4.0) | `roboflow_porch_piracy` | `roboflow_cc_by_4_0` | real |
| Packages (Roboflow Universe, Public Domain) | `roboflow_packages_pd` | `roboflow_public_domain` | real |
| Composites on Playground backgrounds | `synthetic_overlay` | `synthetic` | synthetic |
| Ring Developer Playground live view | `ring_playground` | `ring_playground` | real (simulated device) |

## What "package_count" means

The number of distinct packages (boxes, cases, mailers, padded envelopes, bags, totes) resting at
the door, porch, stoop, step or entry area, and not being held by a person. This matches the
extraction prompt (`v1`: "the number of distinct packages left at the door"). A package that is
only partly in frame still counts.

## Carrier labels

Carrier truth exists only where it is known by construction (synthetic composites). Real images
are not given carrier labels from visual judgement, so carrier accuracy is reported only for
`synthetic_overlay`. For every other source, `carrier` is `null` and is not scored.

## Source 1: Porch Piracy (gluwxyws/porch-piracy, version 8)

Selection is by scene type only. Whether an image looks easy or hard is never a reason to include
or exclude it.

1. Pool: every image in version 8 (2,905 images, no preprocessing or augmentation).
2. Order: the pool is shuffled with a fixed seed (`20261016`) by `eval/build_roboflow.py`. Images are
   reviewed in that order and the first ones that pass the rules are taken, until the quotas below
   are met. Every reviewed image is logged in `eval/labels/review_log.jsonl` with its decision and
   the rule that decided it.
3. Include only frames from a fixed security or doorbell camera looking at an entrance: a door,
   porch, stoop, entry steps, entry walkway, or the hallway outside a unit.
4. Exclude:
   - close-up, studio or hand-held product photos (not a camera view of an entrance);
   - images where an annotated package is being held or carried by a person (the "left at the
     door" count would be ambiguous);
   - exact or near-exact duplicates of an image already taken (same scene and moment, including
     re-encodes and flips); the first one in review order is kept.
5. Quotas: 50 to 60 positives (at least one package in the corrected count) and 10 to 20 negatives
   (zero packages, for example a person-only frame or an empty porch).
6. Count check: for every selected image the bounding-box count is compared with a count made by
   eye under the definition above. If they differ, the eye count becomes the truth and the item
   records `bbox_count`, the corrected `package_count`, and a one-line `correction_reason` (missed
   box, duplicate box, box drawn on a non-package, and so on). Negatives get the same check, which
   catches packages the annotators missed.

## Source 2: Packages (joseph-nelson/packages-g0ton)

All images in the pinned version are reviewed, without sampling (26 images on the dataset page).
Rules 3, 4 and 6 from source 1 apply. No quotas, because the set is small. If the pinned
version is augmented (more images than the 26 originals), only originals are used.

## Source 3: synthetic composites

Built by `eval/build_synthetic.py` with a fixed seed. Labels are exact by construction and are not
checked by eye for correctness, only for visibility (see rule 4).

1. Backgrounds: Ring Playground frames saved in `data/spike/`:
   - driveway (vehicle clip) frames with no vehicle in view: base count 0;
   - porch (package clip) frames: base count 1, because the scene already contains one unlabelled
     box.
2. Boxes: crops of single boxes from the CC BY 4.0 dataset `mpj/package-detection-f03pa`, cut out
   along their bounding boxes and scaled to a plausible size for the background.
3. Each composite is one delivery event: 1 to 5 pasted boxes, all from one carrier. The carrier is
   drawn from UPS, FedEx, USPS, Amazon, DHL, Sysco, US Foods, or none. With none, the boxes have no
   label and the carrier truth is `Unknown`. Labelled boxes get a printed shipping label (carrier
   name in large type, plus a made-up tracking number). No real logos are reproduced.
4. Pasted boxes never overlap each other or the existing porch box, and every label is fully inside
   the frame. A composite where a box or label ended up hidden is regenerated with the next seed,
   and the regeneration is logged. That is the only check made by eye.
5. Truth: `package_count` = base count + pasted boxes; `carrier` = the pasted carrier (`Unknown` if
   none).
6. Size: 36 composites.

## Source 4: Ring Playground

The two simulated live-view events captured with `scripts/ring_spike.py --spread`. Each item is
one model call with 3 frames, as the production pipeline would send:

- `package`: frames 10, 30, 50 of `data/spike/package/`; package_count 1.
- `vehicle`: frames 08, 14, 40 of `data/spike/vehicle/`; package_count 0.

These frames were already used in the 2026-10-15 smoke test, so they are not held out. N=2, which
is reported but supports no accuracy claim on its own.

## Metrics reported per source and model

- package_count exact-match rate, with N and a 95% Wilson interval; mean absolute error;
- false-alarm rate on negatives (predicted count > 0 when truth is 0), with N;
- carrier accuracy, only where carrier truth exists;
- latency p50/p95 (wall clock per call) and cost per call at the verified US East rates.

## Attribution

- Porch Piracy Dataset, gluwxyws, Roboflow Universe, CC BY 4.0,
  https://universe.roboflow.com/gluwxyws/porch-piracy (version 8, licence checked on the dataset
  page on 2026-10-15).
- Package Detection Dataset, MPJ, Roboflow Universe, CC BY 4.0,
  https://universe.roboflow.com/mpj/package-detection-f03pa (box crops used in the synthetic
  composites; modified: cropped, scaled and labelled).
- Packages Dataset, Roboflow Community, Roboflow Universe, Public Domain,
  https://universe.roboflow.com/joseph-nelson/packages-g0ton.

## Amendment 1 (2026-10-16, before any model run)

Why: after download, screening showed the original rules could not be met from the data, for
reasons unrelated to how easy any image is.

- Porch Piracy v8 is an aggregate. Most of its 2,905 images are studio, hand-held or stock photos,
  and it contains many re-encoded or flipped copies of the same photo (same name before `.rf.`).
  Only 31 distinct images from a fixed doorbell or security camera have a package box, so the
  quota of 50 to 60 positives cannot be met.
- The Packages (Public Domain) set is 26 hand-held phone photos at doors. Porch Piracy also
  contains copies of these same photos (the `IMG_`, `PXL_` and `package_` series), so the two sources overlap.
- One Porch Piracy image (`WhatsApp-Image-2026-09-27-at-20-04-07`) is the same footage as the
  Ring Playground "Package" scene.

Changes:

1. Series screening. Porch Piracy file names fall into series by origin (for example `Opeeen_Packages`,
   `pexels-*`, `CCTV_`, `WhatsApp-Image-`). Two positives and one negative from every series with
   five or more images were viewed (`eval.review_sheet --ranks`, sheets not committed) to classify
   the series by scene type:
   - eligible for positives and negatives: `WhatsApp-Image-*` (doorbell and security camera
     screenshots);
   - eligible for negatives only (no package boxes in the series): `CCTV_*`, `<n>_png`,
     `<n>_png_jpg`, `video*_mp4-*`, `IMG_*_mov-*`; each image must still pass rule 3 by eye;
   - excluded, not a fixed camera at an entrance: studio and hand-held series (`*Opeeen*`, `*Booo*`,
     `*Packageee*`, `*packaaaageeeees*`, `*Open_booo*`), stock photos (`pexels-*`, `<n>_jpg` stock
     images), an indoor test camera aimed at desks and room corners (`chn5-*`), an indoor living
     room (`not-fallen*`);
   - excluded as copies of the Public Domain source: `IMG_<n>_jpg`, `PXL_*`, `package_<n>_jpg`.
2. Duplicates are identified by file name before `.rf.`; only the first copy in review order is
   reviewed.
3. Porch Piracy quotas: every eligible positive is reviewed (the pool is exhausted, not sampled),
   and negatives are the first 20 in review order that pass rule 3 and show no package by eye.
4. Porch Piracy images that show the Ring Playground footage are excluded, so the same scene is
   never scored under two sources.
5. Packages (Public Domain): rule 3 becomes "a photo of a door, porch, stoop or entry area; hand-held
   allowed". The row is reported as hand-held photos, not camera footage. All 26 images in version
   6 (the raw upload, no resize or augmentation; version 5 is augmented) are reviewed; rules 4 and 6
   still apply.
