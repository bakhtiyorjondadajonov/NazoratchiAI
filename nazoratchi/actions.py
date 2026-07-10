"""Telegram-side actions with idempotent error handling.

`HIDE_REQUESTER_MISSING` means the join request no longer exists (user
withdrew it, or another admin already acted) — every caller treats that as
"resolved externally", never as a crash.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

log = logging.getLogger(__name__)

_REQUEST_GONE = ("HIDE_REQUESTER_MISSING", "USER_ALREADY_PARTICIPANT")


def _request_gone(e: TelegramBadRequest) -> bool:
    return any(marker in e.message for marker in _REQUEST_GONE)


async def approve_request(bot: Bot, chat_id: int, user_id: int) -> bool:
    """True if approved; False if the request was already gone."""
    try:
        await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        return True
    except TelegramBadRequest as e:
        if _request_gone(e):
            log.info("approve: request gone for user %s in %s", user_id, chat_id)
            return False
        raise


async def decline_request(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        return True
    except TelegramBadRequest as e:
        if _request_gone(e):
            log.info("decline: request gone for user %s in %s", user_id, chat_id)
            return False
        raise


async def ban(bot: Bot, chat_id: int, user_id: int,
              revoke_messages: bool | None = None) -> bool:
    """Permanent ban: the user cannot rejoin until an admin presses Unban.
    revoke_messages=True also deletes the user's messages in the chat (used on
    first-message bans, where the message is almost certainly the ad)."""
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id,
                                  revoke_messages=revoke_messages)
        return True
    except TelegramBadRequest:
        log.exception("ban failed for user %s in %s", user_id, chat_id)
        return False


async def fetch_user_bio(bot: Bot, user_id: int) -> str | None:
    """Bio via getChat — only works once the bot has 'met' the user (e.g. after
    their first group message). Failure is expected for strangers."""
    try:
        chat = await bot.get_chat(user_id)
        return chat.bio
    except Exception:
        log.info("getChat(%s) failed — bio unreadable", user_id)
        return None


async def soft_kick(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Remove a user who already got in. 60s ban = kick that allows rejoining."""
    try:
        await bot.ban_chat_member(
            chat_id=chat_id, user_id=user_id, until_date=int(time.time()) + 60,
        )
        return True
    except TelegramBadRequest:
        log.exception("soft_kick failed for user %s in %s", user_id, chat_id)
        return False


async def unban(bot: Bot, chat_id: int, user_id: int) -> bool:
    """only_if_banned prevents kicking a user who legitimately joined meanwhile."""
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        return True
    except TelegramBadRequest:
        log.exception("unban failed for user %s in %s", user_id, chat_id)
        return False


async def create_single_use_invite(bot: Bot, chat_id: int) -> str | None:
    """One-member, 24h invite link for the admin-override flow. Plain link (no
    join-request mode) so the overridden user gets straight in."""
    try:
        link = await bot.create_chat_invite_link(
            chat_id=chat_id, member_limit=1,
            expire_date=int(time.time()) + 24 * 3600,
            name="nazoratchi override",
        )
        return link.invite_link
    except TelegramBadRequest:
        log.exception("create_single_use_invite failed for chat %s", chat_id)
        return None


async def try_dm(bot: Bot, user_chat_id: int | None, text: str) -> bool:
    """Best-effort DM to a join requester (only works while the request is
    pending, within ~5 minutes). Failure is expected and non-fatal."""
    if not user_chat_id:
        return False
    try:
        await bot.send_message(chat_id=user_chat_id, text=text)
        return True
    except Exception:
        return False
