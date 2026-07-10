"""Local CV models: NudeNet v3 detector (320n) + optional v2 classifier ensemble.

The detector finds body-part classes with scores; the old v2 binary
safe/unsafe classifier is kept as a second opinion because the detector alone
under-recalls (~60% on community benchmarks) and misses drawn/anime content.

Inference is CPU-bound: it runs in a thread pool behind a semaphore so the
event loop never blocks and at most `max_concurrent_inferences` images are
processed at once.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from nazoratchi.config import NudenetCfg

log = logging.getLogger(__name__)


@dataclass
class InferenceResult:
    detections: list[dict]           # [{"class": str, "score": float, "box": [...]}]
    classifier_unsafe: float | None  # None if the classifier is disabled/missing
    error: str | None = None


class NudeNetRuntime:
    def __init__(self, cfg: NudenetCfg):
        self.cfg = cfg
        self._semaphore = asyncio.Semaphore(cfg.max_concurrent_inferences)

        import onnxruntime as ort
        from nudenet import NudeDetector  # heavy import, deferred to construction

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = cfg.intra_op_threads

        kwargs = {"inference_resolution": cfg.inference_resolution}
        if cfg.detector_model:
            kwargs["model_path"] = cfg.detector_model
        self._detector = NudeDetector(**kwargs)
        # NudeDetector doesn't expose SessionOptions — rebuild its session with
        # capped intra-op threads so concurrent inferences can't oversubscribe
        # the CPU (nudenet 3.4.2 stores it as self.onnx_session).
        try:
            import nudenet as _nudenet
            model_path = cfg.detector_model or str(
                Path(_nudenet.__file__).parent / "320n.onnx")
            self._detector.onnx_session = ort.InferenceSession(
                model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        except Exception:
            log.warning("could not cap detector threads; using nudenet defaults",
                        exc_info=True)

        self._classifier = None
        self._classifier_input = None
        if cfg.classifier_enabled:
            path = Path(cfg.classifier_model)
            if path.exists():
                self._classifier = ort.InferenceSession(
                    str(path), sess_options=opts, providers=["CPUExecutionProvider"]
                )
                self._classifier_input = self._classifier.get_inputs()[0].name
                log.info("v2 classifier loaded from %s", path)
            else:
                log.warning(
                    "classifier enabled but %s is missing - running DETECTOR-ONLY. "
                    "Download it from the NudeNet v0 GitHub release.", path
                )

    @property
    def classifier_active(self) -> bool:
        return self._classifier is not None

    async def analyze(self, image_bytes: bytes) -> InferenceResult:
        async with self._semaphore:
            return await asyncio.to_thread(self._analyze_sync, image_bytes)

    def _analyze_sync(self, image_bytes: bytes) -> InferenceResult:
        # NudeDetector.detect wants a path; write to a private temp file.
        fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="gk_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(image_bytes)
            detections = self._detector.detect(tmp_path)
            unsafe = self._classify(tmp_path) if self._classifier else None
            return InferenceResult(detections=detections or [], classifier_unsafe=unsafe)
        except Exception as e:
            log.exception("inference failed")
            return InferenceResult(detections=[], classifier_unsafe=None, error=str(e))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _classify(self, image_path: str) -> float | None:
        """v2 classifier: 256x256 RGB /255, output [unsafe, safe] softmax."""
        try:
            import cv2
            img = cv2.imread(image_path)
            if img is None:
                return None
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            img = cv2.resize(img, (256, 256)).astype(np.float32) / 255.0
            batch = np.expand_dims(img, axis=0)
            out = self._classifier.run(None, {self._classifier_input: batch})[0][0]
            # v2 category order is [unsafe, safe]; verify once with
            # `scripts/calibrate.py probe` on a known-safe image.
            return float(out[0])
        except Exception:
            log.exception("classifier inference failed")
            return None

    def health_check(self) -> bool:
        """Run a test inference at boot; the bot refuses to serve if this fails."""
        try:
            import cv2
            fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="gk_health_")
            try:
                sample = np.full((320, 320, 3), 128, dtype=np.uint8)
                with os.fdopen(fd, "wb") as f:
                    ok, buf = cv2.imencode(".jpg", sample)
                    if not ok:
                        return False
                    f.write(buf.tobytes())
                self._detector.detect(tmp_path)
                if self._classifier:
                    self._classify(tmp_path)
                return True
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:
            log.exception("NudeNet health check failed")
            return False
