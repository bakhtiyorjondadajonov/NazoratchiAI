"""Admin commands: /blocked and /held — on-demand lists of current cases.

Only usable in the admin chat (or by configured admin ids anywhere). Each
entry gets an action button that reuses the existing gk:* callbacks, so auth
and idempotency come for free.
"""

from __future__ import annotations

import html
import json
import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from gatekeeper.config import ConfigHolder
from gatekeeper.db import Database

log = logging.getLogger(__name__)

# action_taken values that mean "currently kept out"
BLOCKED_ACTIONS = ("banned", "declined")
# awaiting an admin decision (pending join request, or banned-pending-review)
HELD_ACTIONS = ("pending", "banned_pending")

MAX_ROWS = 15


_REFUSED = object()  # sentinel: not authorized at all


def build_router(holder: ConfigHolder, db: Database) -> Router:
    router = Router(name="commands")

    async def _scope(msg: Message) -> list[int] | None | object:
        """Which chats this caller may list. None = all (operator),
        list = restricted, _REFUSED = no access."""
        cfg = holder.current
        user = msg.from_user
        if user is None:
            return _REFUSED
        if msg.chat.id == cfg.bot.admin_chat_id or user.id in cfg.bot.admin_user_ids:
            return None  # operator scope: everything
        if msg.chat.type == "private":
            owned = db.groups_owned_by(user.id)
            return owned if owned else _REFUSED
        # inside a group: owner or live admin of THAT group only
        group = db.get_group(msg.chat.id)
        if group is not None and group["owner_user_id"] == user.id:
            return [msg.chat.id]
        try:
            member = await msg.bot.get_chat_member(msg.chat.id, user.id)
            if member.status in ("creator", "administrator"):
                return [msg.chat.id]
        except Exception:
            pass
        return _REFUSED

    @router.message(Command("blocked"))
    async def cmd_blocked(msg: Message) -> None:
        scope = await _scope(msg)
        if scope is _REFUSED:
            return
        rows = db.list_by_action(BLOCKED_ACTIONS, limit=MAX_ROWS, chat_ids=scope)
        await _send_list(
            msg, db, rows,
            title="⛔ Currently blocked",
            empty="No blocked users on record.",
            button=lambda r: ("🔓 Unban", "unban") if r["action_taken"] == "banned"
                             else ("🔓 Override", "override"),
        )

    @router.message(Command("held"))
    async def cmd_held(msg: Message) -> None:
        scope = await _scope(msg)
        if scope is _REFUSED:
            return
        rows = db.list_by_action(HELD_ACTIONS, limit=MAX_ROWS, chat_ids=scope)
        await _send_list(
            msg, db, rows,
            title="⏸ Awaiting review",
            empty="Nothing is awaiting review.",
            button=lambda r: ("✅ Approve", "approve") if r["action_taken"] == "pending"
                             else ("🔓 Unban", "unban"),
        )

    return router


async def _send_list(msg: Message, db: Database, rows, *, title: str, empty: str,
                     button) -> None:
    if not rows:
        await msg.answer(empty)
        return
    lines = [f"<b>{title}</b> (latest {len(rows)}):"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    titles: dict[int, str] = {}
    for r in rows:
        if r["chat_id"] not in titles:
            group = db.get_group(r["chat_id"])
            titles[r["chat_id"]] = (group["title"] if group and group["title"]
                                    else str(r["chat_id"]))
        name = " ".join(filter(None, [r["first_name"], r["last_name"]])) or str(r["user_id"])
        username = f" @{r['username']}" if r["username"] else ""
        age_h = (time.time() - r["resolved_at"]) / 3600
        age = f"{age_h:.0f}h ago" if age_h < 48 else f"{age_h / 24:.0f}d ago"
        lines.append(
            f"• <b>{html.escape(name[:40])}</b>{html.escape(username)}"
            f" (<code>{r['user_id']}</code>) in {html.escape(titles[r['chat_id']][:30])}"
            f" — {_trigger_summary(r['signals_json'])}"
            f" · {r['action_taken']} · {age}"
        )
        label, action = button(r)
        kb_rows.append([InlineKeyboardButton(
            text=f"{label}: {name[:24]}",
            callback_data=f"gk:{action}:{r['id']}",
        )])
    await msg.answer(
        "\n".join(lines)[:4000],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


def _trigger_summary(signals_json: str) -> str:
    try:
        signals = json.loads(signals_json)
        kinds = {s["kind"] for s in signals
                 if s["kind"] not in ("text_soft", "no_photo", "gemini_unavailable")}
        return html.escape(", ".join(sorted(kinds)) or "—")
    except Exception:
        return "—"
