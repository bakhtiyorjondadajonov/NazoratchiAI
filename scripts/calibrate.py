#!/usr/bin/env python3
"""Offline threshold calibration for the NudeNet ensemble.

Workflow (run everything from the project root, inside the venv):

1. Assemble a labeled directory:
       calib_set/
         positive_nude/        photographic nudity
         positive_underwear/   underwear / bikini / lingerie
         negative/             tricky normals: portraits, dresses, gym, beach
   Resize nothing — `scan` downscales to Telegram profile geometry itself.

2. One inference pass (slow, once):
       python scripts/calibrate.py scan calib_set --out detections.jsonl

3. Sweep thresholds offline (instant, repeatable):
       python scripts/calibrate.py sweep detections.jsonl --config config.yaml

4. `probe` one image to eyeball raw model output (also verifies the v2
   classifier's [unsafe, safe] output order on a known-safe image):
       python scripts/calibrate.py probe some_photo.jpg

Pick thresholds with ZERO false negatives on positive_nude (decline tier),
then minimize negative-set fire rate subject to zero FN on positive_underwear
(hold tier). Copy the chosen values into config.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

THRESHOLD_GRID = [round(0.20 + 0.05 * i, 2) for i in range(9)]  # 0.20 .. 0.60


def _load_runtime(config_path: str):
    from gatekeeper.config import load_config
    from gatekeeper.screening.nudenet_runtime import NudeNetRuntime
    cfg = load_config(config_path)
    return cfg, NudeNetRuntime(cfg.nudenet)


def _downscale_to_telegram(path: Path) -> bytes:
    """Telegram serves profile photos at max 640px JPEG — evaluate what the bot
    will actually see, not the original."""
    import cv2
    img = cv2.imread(str(path))
    if img is None:
        raise ValueError(f"unreadable image: {path}")
    h, w = img.shape[:2]
    scale = 640 / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 87])
    if not ok:
        raise ValueError(f"re-encode failed: {path}")
    return buf.tobytes()


def cmd_probe(args) -> None:
    cfg, runtime = _load_runtime(args.config)
    image = _downscale_to_telegram(Path(args.image))
    result = runtime._analyze_sync(image)
    print(f"image: {args.image}")
    print(f"detector ({len(result.detections)} detections):")
    for d in sorted(result.detections, key=lambda d: -d["score"]):
        print(f"  {d['class']:32s} {d['score']:.3f}")
    print(f"classifier unsafe: {result.classifier_unsafe}")
    if result.error:
        print(f"ERROR: {result.error}")


def cmd_scan(args) -> None:
    cfg, runtime = _load_runtime(args.config)
    root = Path(args.directory)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    images = [p for p in root.rglob("*") if p.suffix.lower() in exts]
    if not images:
        sys.exit(f"no images under {root}")
    print(f"scanning {len(images)} images...")
    with open(args.out, "w", encoding="utf-8") as out:
        for i, path in enumerate(sorted(images), 1):
            label = path.relative_to(root).parts[0]
            try:
                image = _downscale_to_telegram(path)
                result = runtime._analyze_sync(image)
                record = {
                    "path": str(path), "label": label,
                    "detections": result.detections,
                    "classifier_unsafe": result.classifier_unsafe,
                    "error": result.error,
                }
            except Exception as e:
                record = {"path": str(path), "label": label, "detections": [],
                          "classifier_unsafe": None, "error": str(e)}
            out.write(json.dumps(record) + "\n")
            if i % 25 == 0:
                print(f"  {i}/{len(images)}")
    print(f"wrote {args.out}")


def cmd_sweep(args) -> None:
    from gatekeeper.config import load_config
    from gatekeeper.screening.verdict import Verdict, decide, evaluate_detections
    from gatekeeper.screening.verdict import Signal, SignalKind

    cfg = load_config(args.config)
    records = [json.loads(line) for line in open(args.jsonl, encoding="utf-8")]
    records = [r for r in records if not r.get("error")]
    by_label: dict[str, list[dict]] = {}
    for r in records:
        by_label.setdefault(r["label"], []).append(r)
    print(f"loaded {len(records)} scans: "
          + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_label.items())))

    # ---- 1. verdicts at CURRENT config thresholds -------------------------
    print("\n=== verdicts at current config thresholds ===")
    print(f"{'label':24s} {'decline':>8s} {'hold':>8s} {'approve':>8s}  (approve on a positive = MISS)")
    for label, rows in sorted(by_label.items()):
        counts = {Verdict.DECLINE: 0, Verdict.HOLD: 0, Verdict.APPROVE: 0}
        for r in rows:
            signals = evaluate_detections(r["detections"], 0, cfg.nudenet)
            unsafe = r.get("classifier_unsafe")
            if unsafe is not None and unsafe >= cfg.nudenet.classifier_unsafe_threshold:
                signals.append(Signal(SignalKind.CLASSIFIER_UNSAFE, "", score=unsafe))
            counts[decide(signals)] += 1
        n = len(rows)
        print(f"{label:24s} {counts[Verdict.DECLINE]:>8d} {counts[Verdict.HOLD]:>8d}"
              f" {counts[Verdict.APPROVE]:>8d}   of {n}")
        misses = [r["path"] for r in rows
                  if decide(evaluate_detections(r["detections"], 0, cfg.nudenet)) == Verdict.APPROVE
                  and (r.get("classifier_unsafe") or 0) < cfg.nudenet.classifier_unsafe_threshold
                  and label.startswith("positive")]
        for m in misses[:15]:
            print(f"    MISS: {m}")

    # ---- 2. per-class marginal sweep ---------------------------------------
    print("\n=== per-class fire rate by threshold (marginal) ===")
    all_classes = sorted({d["class"] for r in records for d in r["detections"]})
    for cls in all_classes:
        interesting = cls in cfg.nudenet.decline or cls in cfg.nudenet.hold or cls == "BELLY_EXPOSED"
        if not interesting:
            continue
        print(f"\n{cls}")
        header = "  label \\ thr        " + "".join(f"{t:>7.2f}" for t in THRESHOLD_GRID)
        print(header)
        for label, rows in sorted(by_label.items()):
            best = [max((d["score"] for d in r["detections"] if d["class"] == cls),
                        default=0.0) for r in rows]
            cells = "".join(f"{sum(b >= t for b in best) / len(best):>7.0%}"
                            for t in THRESHOLD_GRID)
            print(f"  {label:18s}{cells}")

    # ---- 3. classifier sweep ------------------------------------------------
    if any(r.get("classifier_unsafe") is not None for r in records):
        print("\n=== v2 classifier unsafe-score fire rate ===")
        grid = [0.5, 0.6, 0.7, 0.8, 0.9]
        print("  label \\ thr        " + "".join(f"{t:>7.2f}" for t in grid))
        for label, rows in sorted(by_label.items()):
            scores = [r.get("classifier_unsafe") or 0.0 for r in rows]
            cells = "".join(f"{sum(s >= t for s in scores) / len(scores):>7.0%}" for t in grid)
            print(f"  {label:18s}{cells}")

    print("\nGoal: 0 misses on positive_*; then minimize negative fire rate. "
          "Edit config.yaml and re-run sweep (no re-inference needed).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="raw model output for one image")
    p.add_argument("image")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("scan", help="run inference over a labeled directory")
    p.add_argument("directory")
    p.add_argument("--out", default="detections.jsonl")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("sweep", help="threshold sweep over a scan dump")
    p.add_argument("jsonl")
    p.set_defaults(func=cmd_sweep)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
