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

from nazoratchi import routing
from nazoratchi.config import ConfigHolder
from nazoratchi.db import Database
from nazoratchi.notifier import user_link
from nazoratchi.strings import label, t

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

    def _lang_for(msg: Message) -> str:
        """Language of the chat the command was typed in — NOT the visibility
        scope (an operator inside a group must still get that group's language).
        In a DM, a single owned group is unambiguous; otherwise the default."""
        cfg = holder.current
        if msg.chat.type in ("group", "supergroup"):
            return routing.resolve_language(db, cfg, msg.chat.id)
        owned = db.groups_owned_by(msg.from_user.id) if msg.from_user else []
        if len(owned) == 1:
            return routing.resolve_language(db, cfg, owned[0])
        return cfg.default_language

    @router.message(Command("blocked"))
    async def cmd_blocked(msg: Message) -> None:
        scope = await _scope(msg)
        if scope is _REFUSED:
            return
        lang = _lang_for(msg)
        rows = db.list_by_action(BLOCKED_ACTIONS, limit=MAX_ROWS, chat_ids=scope)
        await _send_list(
            msg, db, rows, lang=lang,
            title=t(lang, "list.blocked_title"),
            empty=t(lang, "list.blocked_empty"),
            button=lambda r: (t(lang, "btn.unban_short"), "unban")
                             if r["action_taken"] == "banned"
                             else (t(lang, "btn.override_short"), "override"),
        )

    @router.message(Command("held"))
    async def cmd_held(msg: Message) -> None:
        scope = await _scope(msg)
        if scope is _REFUSED:
            return
        lang = _lang_for(msg)
        rows = db.list_by_action(HELD_ACTIONS, limit=MAX_ROWS, chat_ids=scope)
        await _send_list(
            msg, db, rows, lang=lang,
            title=t(lang, "list.held_title"),
            empty=t(lang, "list.held_empty"),
            button=lambda r: (t(lang, "btn.approve_short"), "approve")
                             if r["action_taken"] == "pending"
                             else (t(lang, "btn.unban_short"), "unban"),
        )

    return router


async def _send_list(msg: Message, db: Database, rows, *, title: str, empty: str,
                     button, lang: str = "en") -> None:
    if not rows:
        await msg.answer(empty)
        return
    lines = [f"<b>{title}</b> {t(lang, 'list.latest', n=len(rows))}:"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    titles: dict[int, str] = {}
    for i, r in enumerate(rows, 1):
        if r["chat_id"] not in titles:
            group = db.get_group(r["chat_id"])
            titles[r["chat_id"]] = (group["title"] if group and group["title"]
                                    else str(r["chat_id"]))
        name = " ".join(filter(None, [r["first_name"], r["last_name"]])) or str(r["user_id"])
        link = user_link(r["user_id"], r["first_name"], r["last_name"], r["username"])
        username = f" — @{html.escape(r['username'])}" if r["username"] else ""
        age_h = (time.time() - r["resolved_at"]) / 3600
        age = (t(lang, "age.hours", n=f"{age_h:.0f}") if age_h < 48
               else t(lang, "age.days", n=f"{age_h / 24:.0f}"))
        lines += [
            "",
            f"{i}. {link}{username}",
            f"   🆔 <code>{r['user_id']}</code> · {html.escape(titles[r['chat_id']][:30])}",
            f"   🚩 {_trigger_summary(r['signals_json'])}"
            f" · {html.escape(label(lang, 'action', r['action_taken']))} · {age}",
        ]
        btn_label, action = button(r)
        kb_rows.append([InlineKeyboardButton(
            text=f"{i}. {btn_label} — {name[:20]}",
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
