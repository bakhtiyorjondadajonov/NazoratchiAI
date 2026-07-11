"""Profile-photo screening: fetch every profile photo, run the NudeNet
ensemble, translate detections into policy signals.

Failure policy: a download/inference failure is never a silent pass — after
retries it becomes a PHOTO_FETCH_FAILED signal, which the verdict table maps
to HOLD (admin decides).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from io import BytesIO

from aiogram import Bot

from nazoratchi.config import AppConfig
from nazoratchi.screening.nudenet_runtime import NudeNetRuntime
from nazoratchi.screening.verdict import Signal, SignalKind, evaluate_detections

log = logging.getLogger(__name__)


@dataclass
class PhotoCheckOutcome:
    signals: list[Signal] = field(default_factory=list)
    flagged_file_ids: list[str] = field(default_factory=list)  # for the admin report
    detection_rows: list[dict] = field(default_factory=list)   # raw dump for calibration
    notes: list[str] = field(default_factory=list)
    photos_scanned: int = 0


async def check_message_photo(
    bot: Bot,
    file_id: str,
    file_unique_id: str | None,
    cfg: AppConfig,
    runtime: NudeNetRuntime,
) -> PhotoCheckOutcome:
    """One-time analysis of a photo POSTED as a member's first message.

    Same ensemble and thresholds as profile photos; signals are labeled
    'message photo:' and stored with photo_index=-1 so the calibration data
    can tell the origins apart. Infra failure (incl. message deleted before
    the worker ran) → PHOTO_FETCH_FAILED, which never bans."""
    outcome = PhotoCheckOutcome()
    image = await _download(bot, file_id, cfg.photos.download_retries)
    if image is None:
        outcome.signals.append(Signal(
            SignalKind.PHOTO_FETCH_FAILED,
            "message photo: download failed after retries", photo_index=-1))
        return outcome
    result = await runtime.analyze(image)
    if result.error:
        outcome.signals.append(Signal(
            SignalKind.PHOTO_FETCH_FAILED,
            f"message photo: inference error: {result.error}", photo_index=-1))
        return outcome
    outcome.photos_scanned = 1

    for det in result.detections:
        outcome.detection_rows.append({
            "photo_index": -1, "file_unique_id": file_unique_id,
            "model": "detector", "class": det["class"],
            "score": float(det["score"]), "box": det.get("box"),
        })
    if result.classifier_unsafe is not None:
        outcome.detection_rows.append({
            "photo_index": -1, "file_unique_id": file_unique_id,
            "model": "classifier", "class": "unsafe",
            "score": result.classifier_unsafe, "box": None,
        })

    photo_signals = evaluate_detections(result.detections, -1, cfg.nudenet)
    if (result.classifier_unsafe is not None
            and result.classifier_unsafe >= cfg.nudenet.classifier_unsafe_threshold):
        photo_signals.append(Signal(
            SignalKind.CLASSIFIER_UNSAFE,
            f"v2 classifier unsafe={result.classifier_unsafe:.2f}",
            score=result.classifier_unsafe, photo_index=-1,
        ))
    for s in photo_signals:
        s.detail = f"message photo: {s.detail}"
        s.extra["origin"] = "message"
    if photo_signals:
        outcome.flagged_file_ids.append(file_id)
    outcome.signals.extend(photo_signals)
    return outcome


async def check_photos(
    bot: Bot,
    user_id: int,
    cfg: AppConfig,
    runtime: NudeNetRuntime,
) -> PhotoCheckOutcome:
    outcome = PhotoCheckOutcome()

    photos = await _fetch_photo_list(bot, user_id, cfg.photos.max_photos)
    if photos is None:
        outcome.signals.append(Signal(
            SignalKind.PHOTO_FETCH_FAILED, "getUserProfilePhotos failed"))
        return outcome
    if not photos:
        outcome.signals.append(Signal(
            SignalKind.NO_PHOTO, "no visible profile photos (none or privacy-hidden)"))
        outcome.notes.append("no visible profile photo - photo axis not screened")
        return outcome

    for index, sizes in enumerate(photos):
        largest = max(sizes, key=lambda s: s.width * s.height)
        image = await _download(bot, largest.file_id, cfg.photos.download_retries)
        if image is None:
            outcome.signals.append(Signal(
                SignalKind.PHOTO_FETCH_FAILED,
                f"photo {index}: download failed after retries", photo_index=index))
            continue

        result = await runtime.analyze(image)
        if result.error:
            outcome.signals.append(Signal(
                SignalKind.PHOTO_FETCH_FAILED,
                f"photo {index}: inference error: {result.error}", photo_index=index))
            continue
        outcome.photos_scanned += 1

        for det in result.detections:
            outcome.detection_rows.append({
                "photo_index": index, "file_unique_id": largest.file_unique_id,
                "model": "detector", "class": det["class"],
                "score": float(det["score"]), "box": det.get("box"),
            })
        if result.classifier_unsafe is not None:
            outcome.detection_rows.append({
                "photo_index": index, "file_unique_id": largest.file_unique_id,
                "model": "classifier", "class": "unsafe",
                "score": result.classifier_unsafe, "box": None,
            })

        photo_signals = evaluate_detections(result.detections, index, cfg.nudenet)
        if (result.classifier_unsafe is not None
                and result.classifier_unsafe >= cfg.nudenet.classifier_unsafe_threshold):
            photo_signals.append(Signal(
                SignalKind.CLASSIFIER_UNSAFE,
                f"v2 classifier unsafe={result.classifier_unsafe:.2f}",
                score=result.classifier_unsafe, photo_index=index,
            ))
        if photo_signals:
            outcome.flagged_file_ids.append(largest.file_id)
        outcome.signals.extend(photo_signals)

    return outcome


async def _fetch_photo_list(bot: Bot, user_id: int, max_photos: int):
    """Return list of photos (each a list of PhotoSize), or None on API failure."""
    photos = []
    offset = 0
    try:
        while len(photos) < max_photos:
            batch = await bot.get_user_profile_photos(
                user_id=user_id, offset=offset, limit=min(100, max_photos - len(photos)),
            )
            photos.extend(batch.photos)
            if len(photos) >= batch.total_count or not batch.photos:
                break
            offset = len(photos)
        return photos[:max_photos]
    except Exception:
        log.exception("getUserProfilePhotos failed for user %s", user_id)
        return None


async def _download(bot: Bot, file_id: str, retries: int) -> bytes | None:
    for attempt in range(retries + 1):
        try:
            file = await bot.get_file(file_id)
            buffer = BytesIO()
            await bot.download_file(file.file_path, destination=buffer)
            return buffer.getvalue()
        except Exception:
            log.warning("download failed for %s (attempt %d/%d)",
                        file_id, attempt + 1, retries + 1, exc_info=True)
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
    return None
