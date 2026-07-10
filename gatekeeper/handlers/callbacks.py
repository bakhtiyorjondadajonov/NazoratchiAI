"""Admin inline-button callbacks.

Security invariants (red-team findings):
- callback_data is attacker-forgeable → NEVER trusted as authorization; the
  presser's user id must be a configured admin or an actual admin of the
  TARGET GROUP (not merely someone inside the report chat);
- every action is idempotent: the persisted admin_messages.state row is the
  single-writer lock, so double-presses and races between two admins resolve
  to one action and a toast for the loser.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from gatekeeper import actions, notifier
from gatekeeper.config import ConfigHolder
from gatekeeper.db import Database
from gatekeeper.logging_setup import log_decision

log = logging.getLogger(__name__)

_VALID_ACTIONS = {"approve", "decline", "override", "unban", "kick", "keep"}


def build_router(holder: ConfigHolder, db: Database) -> Router:
    router = Router(name="callbacks")

    @router.callback_query(F.data.startswith("gk:"))
    async def on_action(cb: CallbackQuery) -> None:
        cfg = holder.current
        try:
            _, action, raw_id = cb.data.split(":", 2)
            screening_id = int(raw_id)
        except ValueError:
            await cb.answer("Malformed action.", show_alert=True)
            return
        if action not in _VALID_ACTIONS:
            await cb.answer("Malformed action.", show_alert=True)
            return

        screening = db.get_screening(screening_id)
        if screening is None:
            await cb.answer("Unknown screening record.", show_alert=True)
            return

        if not await _is_authorized(cb, cfg, db, screening["chat_id"]):
            log.warning("unauthorized callback by %s: %s", cb.from_user.id, cb.data)
            await cb.answer("Not authorized.", show_alert=True)
            return

        # single-writer lock: first press flips state open→resolved
        if not db.resolve_admin_messages(screening_id):
            await cb.answer("Already handled by another admin.")
            return

        admin = cb.from_user
        admin_label = f"@{admin.username}" if admin.username else str(admin.id)
        chat_id, user_id = screening["chat_id"], screening["user_id"]
        bot = cb.bot
        outcome = ""

        try:
            if action == "approve":
                ok = await actions.approve_request(bot, chat_id, user_id)
                if ok:
                    db.allowlist_add(chat_id, user_id, admin_label, "admin approved after hold")
                    outcome = f"✅ Approved by {admin_label} (allowlisted)"
                else:
                    outcome = (f"⚠️ Approve pressed by {admin_label}, but the request "
                               f"no longer exists (withdrawn or handled elsewhere)")
                db.update_action(screening_id, "approved" if ok else "resolved_externally",
                                 admin_label)

            elif action == "decline":
                ok = await actions.decline_request(bot, chat_id, user_id)
                outcome = (f"⛔ Declined by {admin_label}" if ok else
                           f"⚠️ Decline pressed by {admin_label}, but the request was already gone")
                db.update_action(screening_id, "declined" if ok else "resolved_externally",
                                 admin_label)

            elif action in ("override", "unban"):
                unban_ok = await actions.unban(bot, chat_id, user_id)
                if not unban_ok:
                    outcome = (f"⚠️ {admin_label}: unban FAILED — check the bot's ban "
                               f"rights in the group, then press again")
                    db.update_action(screening_id, "override_failed", admin_label)
                else:
                    db.allowlist_add(chat_id, user_id, admin_label, f"admin {action}")
                    invite = await actions.create_single_use_invite(bot, chat_id)
                    if invite is None:
                        outcome = (f"⚠️ {admin_label}: unbanned + allowlisted, but the invite "
                                   f"link could not be created — issue one manually")
                    else:
                        dm_ok = await actions.try_dm(
                            bot, screening["user_chat_id"],
                            f"You have been approved. Join here: {invite}")
                        if dm_ok:
                            outcome = (f"🔓 Override by {admin_label}: user unbanned, "
                                       f"invite sent by DM")
                        else:
                            outcome = (f"🔓 Override by {admin_label}: user unbanned + "
                                       f"allowlisted. DM failed — forward this single-use "
                                       f"link manually:\n{invite}")
                    db.update_action(screening_id, "overridden", admin_label)

            elif action == "kick":  # semantics: permanent ban (restorable via /blocked)
                ok = await actions.ban(bot, chat_id, user_id)
                outcome = (f"🔨 Banned by {admin_label}" if ok else
                           f"⚠️ Ban by {admin_label} FAILED — check bot rights")
                db.update_action(screening_id, "banned" if ok else "ban_failed", admin_label)

            elif action == "keep":
                db.allowlist_add(chat_id, user_id, admin_label, "admin kept flagged member")
                outcome = f"✅ Kept by {admin_label} (allowlisted)"
                db.update_action(screening_id, "kept", admin_label)

        except Exception:
            log.exception("callback action %s failed for screening %d", action, screening_id)
            # give the buttons back so the admin can retry after a transient error
            db.reopen_admin_messages(screening_id)
            await cb.answer(f"Action '{action}' failed — try again.", show_alert=True)
            return

        if action in ("override", "unban") and "FAILED" in outcome:
            # unban failed → reopen the lock and KEEP the buttons for a retry press
            db.reopen_admin_messages(screening_id)
            log_decision({
                "screening_id": screening_id, "chat_id": chat_id, "user_id": user_id,
                "event": "admin_action", "action": action, "by": admin_label,
                "outcome": outcome,
            })
            await cb.answer(outcome[:190], show_alert=True)
            return

        log_decision({
            "screening_id": screening_id, "chat_id": chat_id, "user_id": user_id,
            "event": "admin_action", "action": action, "by": admin_label,
            "outcome": outcome,
        })
        await notifier.edit_resolved(bot, db, screening_id, outcome)
        await cb.answer(html.unescape(outcome.split("\n")[0])[:190])

    return router


async def _is_authorized(cb: CallbackQuery, cfg, db: Database,
                         target_chat_id: int) -> bool:
    """Operator, the group's enabling owner, or a live admin of the group."""
    if cb.from_user.id in cfg.bot.admin_user_ids:
        return True
    group = db.get_group(target_chat_id)
    if group is not None and group["owner_user_id"] == cb.from_user.id:
        return True
    try:
        member = await cb.bot.get_chat_member(target_chat_id, cb.from_user.id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False
