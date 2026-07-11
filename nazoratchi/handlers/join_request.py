"""chat_join_request intake: persist first, then enqueue.

The handler is deliberately thin — all screening logic lives in the
orchestrator. Persisting before enqueueing means a crash between the two
steps is recovered by resume_pending() at next boot.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ChatJoinRequest

from nazoratchi import actions, routing
from nazoratchi.config import ConfigHolder
from nazoratchi.db import Database
from nazoratchi.logging_setup import log_decision
from nazoratchi.screening.orchestrator import Orchestrator
from nazoratchi.strings import t

log = logging.getLogger(__name__)


def build_router(holder: ConfigHolder, db: Database, orchestrator: Orchestrator) -> Router:
    router = Router(name="join_request")

    @router.chat_join_request()
    async def on_join_request(req: ChatJoinRequest) -> None:
        cfg = holder.current
        if not routing.is_group_enabled(db, req.chat.id):
            return  # bot is idle in this chat until /enable

        user = req.from_user
        if db.is_allowlisted(req.chat.id, user.id) or user.id in cfg.whitelist_users:
            if cfg.mode.dry_run:
                action = "dry_run"
            else:
                ok = await actions.approve_request(req.bot, req.chat.id, user.id)
                action = "approved" if ok else "resolved_externally"
            log_decision({
                "chat_id": req.chat.id, "user_id": user.id, "username": user.username,
                "source": "join_request", "verdict": "approve", "action": action,
                "notes": ["allowlisted"],
            })
            return

        # a screening for this user is already queued/processing → don't duplicate
        # (resolved screenings don't block: a withdrawn-then-repeated request
        # must get a fresh screening or it would rot pending forever)
        if db.unresolved_screening_exists(req.chat.id, user.id):
            log.info("duplicate join request from %s in %s ignored", user.id, req.chat.id)
            return

        screening_id = db.create_screening(
            chat_id=req.chat.id, user_id=user.id, source="join_request",
            user_chat_id=req.user_chat_id, bio=req.bio,
            first_name=user.first_name, last_name=user.last_name,
            username=user.username,
        )

        if cfg.mode.notify_pending_user and not cfg.mode.dry_run:
            # must happen BEFORE the request is processed — the DM window
            # closes at approve/decline time
            await actions.try_dm(
                req.bot, req.user_chat_id,
                t(routing.resolve_language(db, cfg, req.chat.id), "user.pending"),
            )

        await orchestrator.enqueue(screening_id)

    return router
