"""First-message screening: re-screen each member ONCE when they send their
first group message.

Why: at open-join time the bot has never 'met' the user, so their bio is
unreadable and a photo swapped right after joining goes unseen. By their first
message both are visible — this catches the porn-ad accounts that join clean
and turn dirty.

Hot path discipline: this handler runs on EVERY group message, so the cheap
in-memory/point-read checks come first and the Telegram API is only touched
once per member, ever. The persisted first_message_checks row is the
once-only guard (restarts don't re-scan); the in-memory LRU just makes the
common case free.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from aiogram import F, Router
from aiogram.types import Message

from gatekeeper import actions, routing
from gatekeeper.config import ConfigHolder
from gatekeeper.db import Database
from gatekeeper.screening.orchestrator import Orchestrator

log = logging.getLogger(__name__)

_LRU_CAP = 50_000


def build_router(holder: ConfigHolder, db: Database, orchestrator: Orchestrator) -> Router:
    router = Router(name="first_message")
    seen: OrderedDict[tuple[int, int], None] = OrderedDict()

    def _lru_add(key: tuple[int, int]) -> None:
        seen[key] = None
        seen.move_to_end(key)
        if len(seen) > _LRU_CAP:
            seen.popitem(last=False)

    @router.message(F.chat.type.in_({"group", "supergroup"}))
    async def on_group_message(msg: Message) -> None:
        user = msg.from_user
        if user is None or user.is_bot:
            return  # channel posts / anonymous admins / bots
        key = (msg.chat.id, user.id)
        if key in seen:
            return
        if not routing.is_group_enabled(db, msg.chat.id):
            return
        if db.first_message_done(*key):
            _lru_add(key)
            return
        # a join-time screening still in flight → defer, retry on next message
        if db.unresolved_screening_exists(*key):
            return

        cfg = holder.current
        if db.is_allowlisted(*key) or user.id in cfg.whitelist_users:
            db.mark_first_message(*key, reason="allowlisted")
            _lru_add(key)
            return
        try:
            member = await msg.bot.get_chat_member(msg.chat.id, user.id)
            if member.status in ("creator", "administrator"):
                db.mark_first_message(*key, reason="admin")
                _lru_add(key)
                return
        except Exception:
            log.warning("get_chat_member failed for %s in %s — deferring",
                        user.id, msg.chat.id)
            return  # defer rather than mis-classify

        if not db.claim_first_message(*key):
            _lru_add(key)  # lost the race to a concurrent message
            return

        # the user has now 'met' the bot, so the bio is (usually) readable
        bio = await actions.fetch_user_bio(msg.bot, user.id)
        screening_id = db.create_screening(
            chat_id=msg.chat.id, user_id=user.id, source="first_message",
            user_chat_id=user.id, bio=bio,
            first_name=user.first_name, last_name=user.last_name,
            username=user.username,
        )
        db.set_first_message_screening(msg.chat.id, user.id, screening_id)
        _lru_add(key)
        log.info("first-message screening %d for user %s in %s (bio %s)",
                 screening_id, user.id, msg.chat.id,
                 "read" if bio else "unreadable")
        await orchestrator.enqueue(screening_id)

    return router
