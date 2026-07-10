"""Signal model and the tiered decision table.

Pure functions only — no I/O — so the whole policy is unit-testable.

Policy (locked with the group owner):
- DECLINE: exposed-nudity detection or a hard text hit. The join request is
  refused automatically; admin gets an override button.
- HOLD: underwear/covered classes, MALE_BREAST_EXPOSED (NudeNet misgenders),
  the belly-combo rule, classifier-unsafe, Gemini adult / Gemini safety-block,
  and every infrastructure failure. The join request is left PENDING so the
  admin's Approve button still works (a decline is final on Telegram).
- APPROVE: nothing fired. Absence of a photo/bio is never punished.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any

from nazoratchi.config import NudenetCfg


class Verdict(str, enum.Enum):
    APPROVE = "approve"
    HOLD = "hold"
    DECLINE = "decline"


class SignalKind(str, enum.Enum):
    # decline tier
    EXPOSED_HIT = "exposed_hit"
    TEXT_HARD = "text_hard"
    # hold tier
    COVERED_HIT = "covered_hit"
    BELLY_COMBO_HIT = "belly_combo_hit"
    CLASSIFIER_UNSAFE = "classifier_unsafe"
    GEMINI_ADULT = "gemini_adult"
    GEMINI_BLOCKED = "gemini_blocked"
    PHOTO_FETCH_FAILED = "photo_fetch_failed"
    INFRA_ERROR = "infra_error"
    # informational (never affect the verdict)
    TEXT_SOFT = "text_soft"
    NO_PHOTO = "no_photo"
    GEMINI_UNAVAILABLE = "gemini_unavailable"


DECLINE_KINDS = {SignalKind.EXPOSED_HIT, SignalKind.TEXT_HARD}
HOLD_KINDS = {
    SignalKind.COVERED_HIT,
    SignalKind.BELLY_COMBO_HIT,
    SignalKind.CLASSIFIER_UNSAFE,
    SignalKind.GEMINI_ADULT,
    SignalKind.GEMINI_BLOCKED,
    SignalKind.PHOTO_FETCH_FAILED,
    SignalKind.INFRA_ERROR,
}


@dataclass
class Signal:
    kind: SignalKind
    detail: str
    score: float | None = None
    photo_index: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


def decide(signals: list[Signal]) -> Verdict:
    kinds = {s.kind for s in signals}
    if kinds & DECLINE_KINDS:
        return Verdict.DECLINE
    if kinds & HOLD_KINDS:
        return Verdict.HOLD
    return Verdict.APPROVE


def evaluate_detections(
    detections: list[dict[str, Any]],
    photo_index: int,
    cfg: NudenetCfg,
) -> list[Signal]:
    """Map one photo's raw NudeNet detections to policy signals.

    `detections` items: {"class": str, "score": float, "box": [...]} as
    returned by NudeDetector.detect().
    """
    signals: list[Signal] = []
    best: dict[str, float] = {}
    for det in detections:
        cls, score = det["class"], float(det["score"])
        if score > best.get(cls, 0.0):
            best[cls] = score

    for cls, threshold in cfg.decline.items():
        if best.get(cls, 0.0) >= threshold:
            signals.append(Signal(
                SignalKind.EXPOSED_HIT, f"{cls}={best[cls]:.2f}",
                score=best[cls], photo_index=photo_index, extra={"class": cls},
            ))
    for cls, threshold in cfg.hold.items():
        if best.get(cls, 0.0) >= threshold:
            signals.append(Signal(
                SignalKind.COVERED_HIT, f"{cls}={best[cls]:.2f}",
                score=best[cls], photo_index=photo_index, extra={"class": cls},
            ))

    # Belly-combo backstop: BELLY_EXPOSED alone is a crop top; BELLY_EXPOSED
    # together with a near-threshold covered class is usually a bikini.
    belly = best.get("BELLY_EXPOSED", 0.0)
    if belly >= cfg.belly_combo.belly_exposed_min:
        for cls, threshold in cfg.hold.items():
            near = threshold - cfg.belly_combo.covered_class_margin
            if near <= best.get(cls, 0.0) < threshold:
                signals.append(Signal(
                    SignalKind.BELLY_COMBO_HIT,
                    f"BELLY_EXPOSED={belly:.2f} + {cls}={best[cls]:.2f} (near threshold)",
                    score=belly, photo_index=photo_index,
                    extra={"class": cls, "belly": belly},
                ))
                break
    return signals
