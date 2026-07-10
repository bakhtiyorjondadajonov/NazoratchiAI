"""Self-serve tenancy: group admins enable/disable the bot for their group.

/enable — sender must be a live admin of the group (or a global operator),
must be reachable by DM (that DM becomes the report destination), and is
subject to a per-owner group cap. /disable — same auth, turns screening off.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from nazoratchi import routing
from nazoratchi.config import ConfigHolder
from nazoratchi.db import Database
from nazoratchi.logging_setup import log_decision

log = logging.getLogger(__name__)


def build_router(holder: ConfigHolder, db: Database) -> Router:
    router = Router(name="tenancy")

    async def _sender_is_group_admin(msg: Message) -> bool:
        cfg = holder.current
        if msg.from_user is None:
            return False
        if msg.from_user.id in cfg.bot.admin_user_ids:
            return True
        try:
            member = await msg.bot.get_chat_member(msg.chat.id, msg.from_user.id)
            return member.status in ("creator", "administrator")
        except Exception:
            return False

    @router.message(Command("enable"), F.chat.type.in_({"group", "supergroup"}))
    async def cmd_enable(msg: Message) -> None:
        cfg = holder.current
        if not await _sender_is_group_admin(msg):
            return
        sender = msg.from_user
        is_operator = sender.id in cfg.bot.admin_user_ids

        if not is_operator:
            owned = db.count_enabled_by_owner(sender.id)
            if owned >= cfg.tenancy.max_groups_per_owner:
                await msg.reply(
                    f"You already run the bot in {owned} groups — that's the limit.")
                return

        # DM probe: the sender's DM is where this group's reports will go,
        # so it must be reachable BEFORE we enable anything.
        try:
            await msg.bot.send_message(
                sender.id,
                f"NazoratchiAI enabled for “{msg.chat.title or msg.chat.id}”.\n"
                f"Screening reports for that group will arrive here. "
                f"Use /blocked and /held to review cases.",
            )
        except Exception:
            me = await msg.bot.me()
            await msg.reply(
                f"I can't message you privately. Open @{me.username}, press "
                f"Start, then run /enable here again.")
            return

        db.enable_group(msg.chat.id, owner_user_id=sender.id,
                        title=msg.chat.title, is_seed=False)
        routing.invalidate(msg.chat.id)
        log_decision({"event": "group_enabled", "chat_id": msg.chat.id,
                      "title": msg.chat.title, "owner": sender.id})

        reply = "✅ NazoratchiAI is ON. New members are screened automatically."
        problems = await routing.check_group_rights(msg.bot, msg.chat.id)
        if problems:
            reply += "\n⚠️ " + "\n⚠️ ".join(problems)
        await msg.reply(reply)

    @router.message(Command("disable"), F.chat.type.in_({"group", "supergroup"}))
    async def cmd_disable(msg: Message) -> None:
        if not await _sender_is_group_admin(msg):
            return
        db.set_group_enabled(msg.chat.id, False)
        routing.invalidate(msg.chat.id)
        log_decision({"event": "group_disabled", "chat_id": msg.chat.id,
                      "by": msg.from_user.id})
        await msg.reply("NazoratchiAI is OFF for this group.")

    return router
