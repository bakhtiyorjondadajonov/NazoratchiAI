"""Self-serve tenancy: group admins enable/disable the bot for their group.

/enable — sender must be a live admin of the group (or a global operator),
must be reachable by DM (that DM becomes the report destination), and is
subject to a per-owner group cap. After enabling, the owner's DM gets a
language chooser (English / Uzbek); the pick stores the group language and
sends the onboarding guide. /language re-opens the chooser; /disable — same
auth as /enable, turns screening off.
"""

from __future__ import annotations

import contextlib
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from nazoratchi import menu, routing
from nazoratchi.config import ConfigHolder
from nazoratchi.db import Database
from nazoratchi.handlers.callbacks import _is_authorized
from nazoratchi.logging_setup import log_decision
from nazoratchi.notifier import user_link
from nazoratchi.strings import LANGS, t

log = logging.getLogger(__name__)


def _lang_kb(chat_id: int) -> InlineKeyboardMarkup:
    """Callback data format: lang:<lang>:<chat_id> — distinct from gk:*."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇬🇧 English", callback_data=f"lang:en:{chat_id}"),
        InlineKeyboardButton(text="🇺🇿 Oʻzbekcha", callback_data=f"lang:uz:{chat_id}"),
    ]])


def _req_kb(chat_id: int) -> InlineKeyboardMarkup:
    """Operator decision buttons on a group request: req:<decision>:<chat_id>."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("en", "btn.req_approve"),
                             callback_data=f"req:approve:{chat_id}"),
        InlineKeyboardButton(text=t("en", "btn.req_reject"),
                             callback_data=f"req:reject:{chat_id}"),
    ]])


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
        # re-enable keeps the previously chosen language; fresh groups → default
        lang = db.group_language(msg.chat.id)
        title = html.escape(str(msg.chat.title or msg.chat.id))

        if not is_operator:
            owned = db.count_enabled_by_owner(sender.id)
            if owned >= cfg.tenancy.max_groups_per_owner:
                await msg.reply(t(lang, "enable.cap", n=owned))
                return

            # operator gate: strangers don't enable — they request. Only an
            # 'approved' group (or an operator) proceeds to the enable flow.
            approval = db.group_approval(msg.chat.id)
            if approval == "pending":
                await msg.reply(t(lang, "req.pending_already"))
                return
            if approval == "rejected":
                await msg.reply(t(lang, "req.rejected"))
                return
            if approval != "approved":
                # the req.sent DM doubles as the reachability probe — the
                # decision must be deliverable to this person later
                try:
                    await msg.bot.send_message(sender.id, t(lang, "req.sent"))
                except Exception:
                    me = await msg.bot.me()
                    await msg.reply(t(lang, "enable.no_dm", bot=me.username))
                    return
                db.request_group(msg.chat.id, owner_user_id=sender.id,
                                 title=msg.chat.title)
                log_decision({"event": "group_requested", "chat_id": msg.chat.id,
                              "title": msg.chat.title, "by": sender.id})
                try:
                    await msg.bot.send_message(
                        cfg.bot.admin_chat_id,
                        t("en", "req.operator_new", title=title,
                          chat_id=msg.chat.id,
                          requester=user_link(sender.id,
                                              getattr(sender, "first_name", None),
                                              getattr(sender, "last_name", None),
                                              getattr(sender, "username", None)),
                          requester_id=sender.id),
                        reply_markup=_req_kb(msg.chat.id))
                except Exception:
                    log.exception("could not deliver group request to operator")
                await msg.reply(t(lang, "req.sent"))
                return

        # DM probe: the sender's DM is where this group's reports will go,
        # so it must be reachable BEFORE we enable anything.
        try:
            await msg.bot.send_message(
                sender.id, t(lang, "enable.dm_probe", title=title))
        except Exception:
            me = await msg.bot.me()
            await msg.reply(t(lang, "enable.no_dm", bot=me.username))
            return

        db.enable_group(msg.chat.id, owner_user_id=sender.id,
                        title=msg.chat.title, is_seed=False)
        routing.invalidate(msg.chat.id)
        log_decision({"event": "group_enabled", "chat_id": msg.chat.id,
                      "title": msg.chat.title, "owner": sender.id})

        # language chooser lands in the owner's DM (probe above just succeeded)
        with contextlib.suppress(Exception):
            await msg.bot.send_message(
                sender.id, t(lang, "lang.choose", title=title),
                reply_markup=_lang_kb(msg.chat.id))

        reply = t(lang, "enable.on")
        problems = await routing.check_group_rights(msg.bot, msg.chat.id, lang)
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
        await msg.reply(t(db.group_language(msg.chat.id), "enable.off"))

    @router.message(Command("language"), F.chat.type.in_({"group", "supergroup"}))
    async def cmd_language_group(msg: Message) -> None:
        if not await _sender_is_group_admin(msg):
            return
        title = html.escape(str(msg.chat.title or msg.chat.id))
        await msg.reply(t(db.group_language(msg.chat.id), "lang.choose", title=title),
                        reply_markup=_lang_kb(msg.chat.id))

    @router.message(Command("language"), F.chat.type == "private")
    async def cmd_language_private(msg: Message) -> None:
        if msg.from_user is None:
            return
        owned = db.groups_owned_by(msg.from_user.id)
        if not owned:
            await msg.answer(t(holder.current.default_language, "lang.no_groups"))
            return
        for chat_id in owned:  # one chooser per owned group, labeled by title
            group = db.get_group(chat_id)
            title = html.escape(str(group["title"] or chat_id))
            await msg.answer(t(group["language"], "lang.choose", title=title),
                             reply_markup=_lang_kb(chat_id))

    @router.callback_query(F.data.startswith("lang:"))
    async def on_language_pick(cb: CallbackQuery) -> None:
        cfg = holder.current
        try:
            _, lang, raw_id = cb.data.split(":", 2)
            chat_id = int(raw_id)
        except ValueError:
            await cb.answer(t("en", "cb.malformed"), show_alert=True)
            return
        if lang not in LANGS:
            await cb.answer(t("en", "cb.malformed"), show_alert=True)
            return
        if not await _is_authorized(cb, cfg, db, chat_id):
            await cb.answer(t(lang, "cb.not_authorized"), show_alert=True)
            return

        db.set_group_language(chat_id, lang)
        routing.invalidate(chat_id)
        log_decision({"event": "group_language", "chat_id": chat_id,
                      "language": lang, "by": cb.from_user.id})

        # per-chat menu overrides follow the pick (cosmetic — best effort)
        with contextlib.suppress(Exception):
            await menu.set_group_menu(cb.bot, chat_id, lang)
        if cb.message.chat.type == "private":
            with contextlib.suppress(Exception):
                await menu.set_private_menu(cb.bot, cb.message.chat.id, lang)

        group = db.get_group(chat_id)
        title = html.escape(str(group["title"] if group and group["title"] else chat_id))
        confirmation = t(lang, "lang.set", title=title, language=t(lang, "lang.name"))
        with contextlib.suppress(Exception):
            await cb.message.edit_text(confirmation)
        # the guide goes to DMs only — a group /language pick just confirms
        if cb.message.chat.type == "private":
            with contextlib.suppress(Exception):
                await cb.bot.send_message(cb.message.chat.id, t(lang, "onboarding"))
        await cb.answer(t(lang, "lang.name"))

    @router.callback_query(F.data.startswith("req:"))
    async def on_group_request(cb: CallbackQuery) -> None:
        cfg = holder.current
        if cb.from_user.id not in cfg.bot.admin_user_ids:  # operator-only
            await cb.answer(t("en", "cb.not_authorized"), show_alert=True)
            return
        try:
            _, decision, raw_id = cb.data.split(":", 2)
            chat_id = int(raw_id)
        except ValueError:
            await cb.answer(t("en", "cb.malformed"), show_alert=True)
            return
        if decision not in ("approve", "reject"):
            await cb.answer(t("en", "cb.malformed"), show_alert=True)
            return

        group = db.get_group(chat_id)
        if group is None or group["approval"] != "pending":
            await cb.answer(t("en", "req.already_decided"))  # double-press guard
            return
        owner = group["owner_user_id"]
        lang = group["language"]
        title = html.escape(str(group["title"] or chat_id))

        if decision == "approve":
            db.enable_group(chat_id, owner_user_id=owner, title=group["title"])
            routing.invalidate(chat_id)
            with contextlib.suppress(Exception):
                await cb.message.edit_text(t("en", "req.operator_approved", title=title))
            # tell the requester + start the normal onboarding (language pick)
            with contextlib.suppress(Exception):
                await cb.bot.send_message(owner, t(lang, "req.approved_user", title=title))
                await cb.bot.send_message(owner, t(lang, "lang.choose", title=title),
                                          reply_markup=_lang_kb(chat_id))
        else:
            db.set_group_approval(chat_id, "rejected")
            with contextlib.suppress(Exception):
                await cb.message.edit_text(t("en", "req.operator_rejected", title=title))
            with contextlib.suppress(Exception):
                await cb.bot.send_message(owner, t(lang, "req.rejected_user", title=title))

        log_decision({"event": f"group_request_{decision}d", "chat_id": chat_id,
                      "owner": owner, "by": cb.from_user.id})
        await cb.answer("✅" if decision == "approve" else "❌")

    return router
