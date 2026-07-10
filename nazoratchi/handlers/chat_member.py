"""Primary path: users who enter the group (open join, invite link, direct
add) are screened right after entry.

Also maintains the groups table: the bot stays idle in chats until a group
admin runs /enable (it no longer leaves unknown chats).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

from nazoratchi import routing
from nazoratchi.config import ConfigHolder
from nazoratchi.db import Database
from nazoratchi.logging_setup import log_decision
from nazoratchi.screening.orchestrator import Orchestrator

log = logging.getLogger(__name__)


def build_router(holder: ConfigHolder, db: Database, orchestrator: Orchestrator) -> Router:
    router = Router(name="chat_member")

    @router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
    async def on_member_joined(event: ChatMemberUpdated) -> None:
        cfg = holder.current
        if not routing.is_group_enabled(db, event.chat.id):
            return
        user = event.new_chat_member.user
        if user.is_bot:
            return
        # joins produced by an approved join request were already screened
        # (via_join_request only covers DIRECT requests, not invite-link ones)
        if getattr(event, "via_join_request", False):
            return
        if db.is_allowlisted(event.chat.id, user.id) or user.id in cfg.whitelist_users:
            return
        # our own approve triggers a chat_member join echo too — the decision
        # record is the reliable guard (screening age is not: queue latency
        # under a raid can exceed any small window)
        if db.unresolved_screening_exists(event.chat.id, user.id):
            return
        if db.recently_admitted(event.chat.id, user.id, within_s=24 * 3600):
            return

        screening_id = db.create_screening(
            chat_id=event.chat.id, user_id=user.id, source="chat_member",
            user_chat_id=None, bio=None,  # bio unreadable for strangers on this path
            first_name=user.first_name, last_name=user.last_name,
            username=user.username,
        )
        log.info("fallback screening %d for user %s who entered %s without a request",
                 screening_id, user.id, event.chat.id)
        await orchestrator.enqueue(screening_id)

    @router.my_chat_member()
    async def on_my_membership(event: ChatMemberUpdated) -> None:
        chat = event.chat
        if chat.type == "private":
            return
        new_status = event.new_chat_member.status
        if new_status in ("member", "administrator", "restricted"):
            # stay idle until a group admin runs /enable
            db.ensure_group(chat.id, chat.title)
            log.info("present in chat %s (%s) — status %s", chat.id, chat.title,
                     new_status)
        elif new_status in ("left", "kicked"):
            db.set_group_enabled(chat.id, False)
            routing.invalidate(chat.id)
            log_decision({"event": "removed_from_chat", "chat_id": chat.id,
                          "title": chat.title})

    return router
