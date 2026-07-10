"""Multi-tenancy helpers: which groups are active, where their reports go,
and whether the bot has the rights it needs in a group.

Both lookups sit on hot paths (every group message / every report), so they
are cached with a short TTL; /enable and /disable call invalidate() so
changes take effect immediately.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot

from gatekeeper.config import AppConfig
from gatekeeper.db import Database

log = logging.getLogger(__name__)

_TTL = 60.0
_enabled_cache: dict[int, tuple[float, bool]] = {}
_dest_cache: dict[int, tuple[float, int]] = {}


def invalidate(chat_id: int) -> None:
    _enabled_cache.pop(chat_id, None)
    _dest_cache.pop(chat_id, None)


def is_group_enabled(db: Database, chat_id: int) -> bool:
    now = time.monotonic()
    hit = _enabled_cache.get(chat_id)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    row = db.get_group(chat_id)
    enabled = bool(row and row["enabled"])
    _enabled_cache[chat_id] = (now, enabled)
    return enabled


def resolve_report_chat(db: Database, cfg: AppConfig, chat_id: int) -> int:
    """Owned group → owner's DM; seed / unowned / unknown → operator chat."""
    now = time.monotonic()
    hit = _dest_cache.get(chat_id)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    row = db.get_group(chat_id)
    if row and row["owner_user_id"] and not row["is_seed"]:
        dest = row["owner_user_id"]
    else:
        dest = cfg.bot.admin_chat_id
    _dest_cache[chat_id] = (now, dest)
    return dest


async def check_group_rights(bot: Bot, chat_id: int) -> list[str]:
    """Human-readable problems with the bot's rights in a group (empty = OK)."""
    problems: list[str] = []
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status != "administrator":
            problems.append(f"bot is not an administrator in chat {chat_id}")
        else:
            if not getattr(member, "can_restrict_members", False):
                problems.append(f"bot lacks 'Ban users' right in chat {chat_id} "
                                f"— it cannot remove anyone")
            if not getattr(member, "can_invite_users", False):
                problems.append(f"bot lacks 'Invite users' right in chat {chat_id} "
                                f"— join-request mode and override invite links "
                                f"will not work")
    except Exception as e:
        problems.append(f"cannot inspect chat {chat_id}: {e}")
    return problems
