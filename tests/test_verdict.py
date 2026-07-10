import pytest

from gatekeeper.config import BellyComboCfg, NudenetCfg
from gatekeeper.screening.verdict import (
    Signal,
    SignalKind,
    Verdict,
    decide,
    evaluate_detections,
)


@pytest.fixture
def nn_cfg() -> NudenetCfg:
    return NudenetCfg(
        decline={
            "FEMALE_GENITALIA_EXPOSED": 0.25,
            "MALE_GENITALIA_EXPOSED": 0.25,
            "ANUS_EXPOSED": 0.25,
            "FEMALE_BREAST_EXPOSED": 0.28,
            "BUTTOCKS_EXPOSED": 0.28,
        },
        hold={
            "FEMALE_GENITALIA_COVERED": 0.30,
            "ANUS_COVERED": 0.30,
            "FEMALE_BREAST_COVERED": 0.40,
            "BUTTOCKS_COVERED": 0.45,
            "MALE_BREAST_EXPOSED": 0.45,
        },
        belly_combo=BellyComboCfg(belly_exposed_min=0.50, covered_class_margin=0.10),
        ignore=["FACE_FEMALE", "FACE_MALE", "FEET_EXPOSED", "FEET_COVERED",
                "ARMPITS_EXPOSED", "ARMPITS_COVERED", "BELLY_COVERED"],
    )


def det(cls: str, score: float) -> dict:
    return {"class": cls, "score": score, "box": [0, 0, 10, 10]}


# --- decide() precedence ------------------------------------------------------

def test_no_signals_approves():
    assert decide([]) == Verdict.APPROVE


def test_informational_signals_approve():
    signals = [
        Signal(SignalKind.NO_PHOTO, ""),
        Signal(SignalKind.TEXT_SOFT, ""),
        Signal(SignalKind.GEMINI_UNAVAILABLE, ""),
    ]
    assert decide(signals) == Verdict.APPROVE


def test_covered_holds():
    assert decide([Signal(SignalKind.COVERED_HIT, "")]) == Verdict.HOLD


def test_exposed_declines():
    assert decide([Signal(SignalKind.EXPOSED_HIT, "")]) == Verdict.DECLINE


def test_decline_beats_hold():
    signals = [Signal(SignalKind.COVERED_HIT, ""), Signal(SignalKind.EXPOSED_HIT, "")]
    assert decide(signals) == Verdict.DECLINE


def test_gemini_adult_holds():
    assert decide([Signal(SignalKind.GEMINI_ADULT, "")]) == Verdict.HOLD


def test_gemini_block_holds():
    assert decide([Signal(SignalKind.GEMINI_BLOCKED, "")]) == Verdict.HOLD


def test_infra_failure_holds_never_approves():
    assert decide([Signal(SignalKind.PHOTO_FETCH_FAILED, "")]) == Verdict.HOLD
    assert decide([Signal(SignalKind.INFRA_ERROR, "")]) == Verdict.HOLD


def test_hard_text_declines():
    assert decide([Signal(SignalKind.TEXT_HARD, "")]) == Verdict.DECLINE


# --- evaluate_detections ------------------------------------------------------

def test_exposed_above_threshold(nn_cfg):
    signals = evaluate_detections([det("FEMALE_BREAST_EXPOSED", 0.30)], 0, nn_cfg)
    assert [s.kind for s in signals] == [SignalKind.EXPOSED_HIT]


def test_exposed_below_threshold(nn_cfg):
    assert evaluate_detections([det("FEMALE_BREAST_EXPOSED", 0.20)], 0, nn_cfg) == []


def test_covered_above_threshold(nn_cfg):
    signals = evaluate_detections([det("FEMALE_BREAST_COVERED", 0.55)], 0, nn_cfg)
    assert [s.kind for s in signals] == [SignalKind.COVERED_HIT]


def test_covered_below_threshold_dress_passes(nn_cfg):
    # a weak covered-breast hit on an evening dress must NOT flag
    assert evaluate_detections([det("FEMALE_BREAST_COVERED", 0.32)], 0, nn_cfg) == []


def test_male_breast_holds_at_high_threshold(nn_cfg):
    assert evaluate_detections([det("MALE_BREAST_EXPOSED", 0.40)], 0, nn_cfg) == []
    signals = evaluate_detections([det("MALE_BREAST_EXPOSED", 0.50)], 0, nn_cfg)
    assert [s.kind for s in signals] == [SignalKind.COVERED_HIT]


def test_ignored_classes_never_signal(nn_cfg):
    dets = [det("FACE_FEMALE", 0.99), det("ARMPITS_EXPOSED", 0.95),
            det("BELLY_COVERED", 0.90), det("FEET_EXPOSED", 0.88)]
    assert evaluate_detections(dets, 0, nn_cfg) == []


def test_belly_alone_passes_crop_top(nn_cfg):
    assert evaluate_detections([det("BELLY_EXPOSED", 0.80)], 0, nn_cfg) == []


def test_belly_combo_catches_bikini(nn_cfg):
    # covered class NEAR (but below) its threshold + strong belly = bikini backstop
    dets = [det("BELLY_EXPOSED", 0.60), det("FEMALE_BREAST_COVERED", 0.35)]
    signals = evaluate_detections(dets, 0, nn_cfg)
    assert [s.kind for s in signals] == [SignalKind.BELLY_COMBO_HIT]


def test_belly_combo_not_triggered_by_far_below(nn_cfg):
    dets = [det("BELLY_EXPOSED", 0.60), det("FEMALE_BREAST_COVERED", 0.25)]
    assert evaluate_detections(dets, 0, nn_cfg) == []


def test_belly_combo_absent_when_covered_over_threshold(nn_cfg):
    # covered class over threshold is already a COVERED_HIT; no double count
    dets = [det("BELLY_EXPOSED", 0.60), det("FEMALE_BREAST_COVERED", 0.45)]
    kinds = [s.kind for s in evaluate_detections(dets, 0, nn_cfg)]
    assert kinds == [SignalKind.COVERED_HIT]


def test_multiple_detections_take_best_score(nn_cfg):
    dets = [det("FEMALE_BREAST_EXPOSED", 0.10), det("FEMALE_BREAST_EXPOSED", 0.35)]
    signals = evaluate_detections(dets, 0, nn_cfg)
    assert signals and signals[0].score == 0.35
