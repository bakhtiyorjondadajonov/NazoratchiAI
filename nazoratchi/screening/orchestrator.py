"""Screening orchestrator: bounded worker pool over a FIFO queue.

Every screening is persisted BEFORE it is enqueued (the handlers guarantee
that), so a crash or restart never loses a pending decision: on boot,
`resume_pending()` re-enqueues everything unresolved.

Check pipeline seam: `_run_checks` iterates the configured check callables
(photo, text). A future stories check (MTProto sidecar) plugs in there and
feeds the same Signal types.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiogram import Bot

from nazoratchi import actions, notifier
from nazoratchi.config import ConfigHolder
from nazoratchi.db import Database
from nazoratchi.logging_setup import log_decision
from nazoratchi.screening.gemini_check import GeminiChecker
from nazoratchi.screening.nudenet_runtime import NudeNetRuntime
from nazoratchi.screening.photo_check import check_message_photo, check_photos
from nazoratchi.screening.text_check import TextChecker, check_fields
from nazoratchi.screening.verdict import Signal, SignalKind, Verdict, decide

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        holder: ConfigHolder,
        runtime: NudeNetRuntime,
        gemini: GeminiChecker,
    ):
        self.bot = bot
        self.db = db
        self.holder = holder
        self.runtime = runtime
        self.gemini = gemini
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._flood_alerted = False
        self._text_checker: TextChecker | None = None
        self._text_cfg = None  # object reference, not id(): survives GC address reuse

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        for i in range(self.holder.current.queue.workers):
            self._workers.append(asyncio.create_task(self._worker(i)))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    def resume_pending(self) -> int:
        rows = self.db.unresolved_screenings()
        for row in rows:
            self.queue.put_nowait(row["id"])
        return len(rows)

    # -- intake --------------------------------------------------------------

    async def enqueue(self, screening_id: int) -> None:
        self.queue.put_nowait(screening_id)
        depth = self.queue.qsize()
        cfg = self.holder.current
        if depth >= cfg.queue.flood_alert_depth and not self._flood_alerted:
            self._flood_alerted = True
            await notifier.send_alert(
                self.bot, cfg,
                f"⚠️ Screening queue depth is {depth} — possible join flood/raid. "
                f"Requests are safe (persisted) and will be processed in order.",
            )
        elif depth < cfg.queue.flood_alert_depth // 2:
            self._flood_alerted = False

    # -- workers -------------------------------------------------------------

    async def _worker(self, n: int) -> None:
        while True:
            screening_id = await self.queue.get()
            try:
                await self._process(screening_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker %d: unhandled error on screening %d", n, screening_id)
                # fail-safe: HOLD + admin alert; never silently accept or decline
                try:
                    await self._fail_safe(screening_id)
                except Exception:
                    log.exception("fail-safe handling failed for screening %d", screening_id)
            finally:
                self.queue.task_done()

    def _get_text_checker(self) -> TextChecker:
        cfg = self.holder.current
        if self._text_checker is None or self._text_cfg is not cfg:
            self._text_checker = TextChecker(cfg.text)
            self._text_cfg = cfg
        return self._text_checker

    async def _process(self, screening_id: int) -> None:
        row = self.db.get_screening(screening_id)
        if row is None or row["status"] == "resolved":
            return
        cfg = self.holder.current
        self.db.set_status(screening_id, "processing")

        signals: list[Signal] = []
        notes: list[str] = []
        gemini_reason: str | None = None

        # --- first-message content payload (persisted by the handler) ---
        ctx = json.loads(row["context_json"]) if row["context_json"] else None
        if ctx and not cfg.mode.check_first_message_content:
            notes.append("message content check disabled by config")
            ctx = None

        # --- text check (bio + names + username; red-team finding: porn bots
        # put the ad in the name at least as often as in the bio) ---
        fields = {
            "bio": row["bio"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "username": row["username"],
        }
        if ctx and ctx.get("text"):
            fields["message"] = ctx["text"]  # content joins the same pipeline
        text_result = check_fields(self._get_text_checker(), fields)
        for hit in text_result.hard_hits:
            signals.append(Signal(SignalKind.TEXT_HARD, hit))
        for hit in text_result.soft_hits:
            signals.append(Signal(SignalKind.TEXT_SOFT, hit))

        if text_result.needs_gemini:
            outcome = await self.gemini.classify(fields)
            if outcome.status == "ok" and outcome.verdict and outcome.verdict.is_adult:
                gemini_reason = outcome.verdict.reason
                signals.append(Signal(
                    SignalKind.GEMINI_ADULT,
                    f"Gemini: {outcome.verdict.reason}",
                    score=outcome.verdict.confidence,
                ))
            elif outcome.status == "blocked":
                gemini_reason = outcome.verdict.reason if outcome.verdict else "safety block"
                signals.append(Signal(
                    SignalKind.GEMINI_BLOCKED, f"Gemini: {gemini_reason}", score=0.9))
            elif outcome.status in ("unavailable", "disabled"):
                signals.append(Signal(
                    SignalKind.GEMINI_UNAVAILABLE,
                    "Gemini unavailable - text verdict is regex-only"))
                notes.append("Gemini unavailable — text verdict is regex-only")

        # --- photo check (profile) ---
        photo_outcome = await check_photos(self.bot, row["user_id"], cfg, self.runtime)
        signals.extend(photo_outcome.signals)
        notes.extend(photo_outcome.notes)
        if row["source"] == "chat_member":
            notes.append("screened at join — bio not readable, names only")
        elif row["source"] == "first_message":
            notes.append("first-message re-screen — bio "
                         + ("read" if row["bio"] else "unreadable"))
        self.db.add_detections(screening_id, photo_outcome.detection_rows)

        # --- message content: posted photo ---
        flagged_file_ids = list(photo_outcome.flagged_file_ids)
        photos_scanned = photo_outcome.photos_scanned
        if ctx and ctx.get("photo_file_id"):
            msg_outcome = await check_message_photo(
                self.bot, ctx["photo_file_id"], ctx.get("photo_unique_id"),
                cfg, self.runtime)
            signals.extend(msg_outcome.signals)
            notes.extend(msg_outcome.notes)
            self.db.add_detections(screening_id, msg_outcome.detection_rows)
            flagged_file_ids.extend(msg_outcome.flagged_file_ids)
            photos_scanned += msg_outcome.photos_scanned
        if ctx and ctx.get("text") and any(
                h.startswith("message:") for h in
                (*text_result.hard_hits, *text_result.soft_hits)):
            notes.append(f"msg: {ctx['text'][:120]}")

        # --- verdict + action ---
        verdict = decide(signals)
        action_taken = await self._apply(row, verdict, cfg, signals)

        self.db.record_decision(
            screening_id,
            verdict=verdict.value,
            signals=[s.to_dict() for s in signals],
            action_taken=action_taken,
        )
        # HOLD (and fallback-path DECLINE with its Unban button) stays open for
        # admin action; the screening row is resolved to keep the queue clean —
        # the admin_messages/decisions rows carry the open/closed state.
        self.db.set_status(screening_id, "resolved")

        log_decision({
            "screening_id": screening_id,
            "chat_id": row["chat_id"], "user_id": row["user_id"],
            "username": row["username"], "source": row["source"],
            "verdict": verdict.value, "action": action_taken,
            "dry_run": cfg.mode.dry_run,
            "signals": [s.to_dict() for s in signals],
            "photos_scanned": photos_scanned,
            "notes": notes,
        })

        # A report failure must never look like a screening failure: the
        # action is already applied and recorded — _fail_safe would rewrite
        # a real decision as a fake HOLD.
        try:
            await notifier.report(
                bot=self.bot, cfg=cfg, db=self.db,
                screening=row, verdict=verdict, signals=signals,
                flagged_file_ids=flagged_file_ids,
                notes=notes, action_taken=action_taken,
            )
        except Exception:
            log.exception("admin report failed for screening %d (decision %s/%s stands)",
                          screening_id, verdict.value, action_taken)

    # HOLD caused by these kinds = the profile itself looks bad → ban on the
    # open-join path. HOLD caused only by infra failure must never ban anyone.
    _CONTENT_HOLD_KINDS = {
        SignalKind.COVERED_HIT, SignalKind.BELLY_COMBO_HIT,
        SignalKind.CLASSIFIER_UNSAFE, SignalKind.GEMINI_ADULT,
        SignalKind.GEMINI_BLOCKED,
    }

    async def _apply(self, row, verdict: Verdict, cfg, signals: list[Signal]) -> str:
        """Execute the Telegram-side action for a verdict. Returns action_taken."""
        if cfg.mode.dry_run:
            return "dry_run"
        chat_id, user_id = row["chat_id"], row["user_id"]
        if row["source"] == "join_request":
            if verdict == Verdict.APPROVE:
                ok = await actions.approve_request(self.bot, chat_id, user_id)
                return "approved" if ok else "resolved_externally"
            if verdict == Verdict.DECLINE:
                ok = await actions.decline_request(self.bot, chat_id, user_id)
                return "declined" if ok else "resolved_externally"
            return "pending"  # HOLD: request deliberately left pending
        else:  # chat_member / first_message: the user is already inside
            # Bad profile → permanent ban, restorable by the admin Unban
            # button (a kick would let a porn bot walk straight back in).
            # First-message bans also delete the user's message — it is
            # almost certainly the ad.
            revoke = True if row["source"] == "first_message" else None
            if verdict == Verdict.DECLINE:
                ok = await actions.ban(self.bot, chat_id, user_id,
                                       revoke_messages=revoke)
                return "banned" if ok else "ban_failed"
            if verdict == Verdict.HOLD:
                if any(s.kind in self._CONTENT_HOLD_KINDS for s in signals):
                    ok = await actions.ban(self.bot, chat_id, user_id,
                                           revoke_messages=revoke)
                    return "banned_pending" if ok else "ban_failed"
                # infra-failure hold: never ban over a network blip
                return "kept_flagged"
            return "kept"

    async def _fail_safe(self, screening_id: int) -> None:
        row = self.db.get_screening(screening_id)
        if row is None:
            return
        if self.db.get_decision(screening_id) is not None:
            # a real decision was already recorded — never overwrite it
            log.error("late failure after decision on screening %d; decision stands",
                      screening_id)
            self.db.set_status(screening_id, "resolved")
            return
        cfg = self.holder.current
        signal = Signal(SignalKind.INFRA_ERROR, "unhandled error during screening")
        self.db.record_decision(
            screening_id, verdict=Verdict.HOLD.value,
            signals=[signal.to_dict()], action_taken="pending",
        )
        self.db.set_status(screening_id, "resolved")
        log_decision({
            "screening_id": screening_id, "chat_id": row["chat_id"],
            "user_id": row["user_id"], "verdict": "hold",
            "action": "pending", "signals": [signal.to_dict()],
            "notes": ["fail-safe: unhandled error"],
        })
        await notifier.report(
            bot=self.bot, cfg=cfg, db=self.db, screening=row,
            verdict=Verdict.HOLD, signals=[signal],
            flagged_file_ids=[], notes=["fail-safe: screening crashed — manual review"],
            action_taken="pending",
        )
