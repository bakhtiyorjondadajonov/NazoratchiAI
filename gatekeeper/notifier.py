"""Admin reports: evidence photo(s) + verdict caption + action buttons.

Telegram constraints honored here:
- media groups cannot carry inline keyboards → album first, then a separate
  keyboard message;
- captions are ≤1024 chars after entity parsing;
- all user-supplied text is HTML-escaped; if Telegram still rejects the
  entities the send is retried without parse_mode (a report must never be
  silently lost).
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

from gatekeeper.config import AppConfig
from gatekeeper.db import Database
from gatekeeper.screening.verdict import Signal, SignalKind, Verdict

log = logging.getLogger(__name__)

VERDICT_BADGE = {
    Verdict.APPROVE: "✅ APPROVED",
    Verdict.HOLD: "⏸ HELD FOR REVIEW",
    Verdict.DECLINE: "⛔ DECLINED",
}


def _kb(verdict: Verdict, source: str, screening_id: int, dry_run: bool,
        action_taken: str = "") -> InlineKeyboardMarkup | None:
    """Callback data format: gk:<action>:<screening_id> (well under 64 bytes)."""
    def btn(text: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"gk:{action}:{screening_id}")

    if verdict == Verdict.APPROVE and not dry_run:
        return None
    if source == "join_request":
        if dry_run or verdict == Verdict.HOLD:
            # request is still pending (dry-run never touched it) — both work
            return InlineKeyboardMarkup(inline_keyboard=[[
                btn("✅ Approve", "approve"), btn("⛔ Decline", "decline"),
            ]])
        if verdict == Verdict.DECLINE:
            return InlineKeyboardMarkup(inline_keyboard=[[
                btn("🔓 Override: let user in", "override"),
            ]])
    else:  # open-join path: there is no join request to approve/decline
        if action_taken in ("banned", "banned_pending"):
            return InlineKeyboardMarkup(inline_keyboard=[[
                btn("🔓 Unban / let back in", "unban"),
            ]])
        if dry_run or verdict in (Verdict.HOLD, Verdict.DECLINE):
            # user is still inside (dry-run, infra-failure hold, or ban failed)
            return InlineKeyboardMarkup(inline_keyboard=[[
                btn("🔨 Ban", "kick"), btn("✅ Keep", "keep"),
            ]])
    return None


def _caption(screening, verdict: Verdict, signals: list[Signal],
             notes: list[str], action_taken: str, dry_run: bool) -> str:
    """Every line is a self-contained HTML fragment, and the caption is
    assembled line-by-line within the 1024-char budget — a blind slice could
    cut inside a tag or entity and make Telegram reject the whole message."""
    e = html.escape
    name = " ".join(filter(None, [screening["first_name"], screening["last_name"]])) or "—"
    username = f"@{screening['username']}" if screening["username"] else "no username"
    lines = [
        f"{'🧪 DRY RUN — would be: ' if dry_run else ''}{VERDICT_BADGE[verdict]}",
        f"<b>{e(name[:64])}</b> ({e(username)}, id <code>{screening['user_id']}</code>)",
        f"chat: <code>{screening['chat_id']}</code> · via {screening['source']}"
        f" · action: {e(action_taken)}",
    ]
    triggers = [s for s in signals if s.kind not in
                (SignalKind.TEXT_SOFT, SignalKind.NO_PHOTO, SignalKind.GEMINI_UNAVAILABLE)]
    if triggers:
        lines.append("<b>Triggered:</b>")
        lines += [f"• {e(s.detail[:120])}" for s in triggers[:8]]
        if len(triggers) > 8:
            lines.append(f"… and {len(triggers) - 8} more (see logs)")
    soft = [s for s in signals if s.kind == SignalKind.TEXT_SOFT]
    if soft:
        lines.append("<i>Soft text signals:</i> " + e("; ".join(s.detail for s in soft[:5])[:200]))
    if screening["bio"]:
        lines.append(f"<b>Bio:</b> <pre>{e(screening['bio'][:120])}</pre>")
    for note in notes:
        lines.append(f"ℹ️ {e(note[:150])}")

    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > 1024:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


async def report(
    *, bot: Bot, cfg: AppConfig, db: Database, screening, verdict: Verdict,
    signals: list[Signal], flagged_file_ids: list[str], notes: list[str],
    action_taken: str,
) -> None:
    from gatekeeper import routing  # local import: routing imports config, not notifier

    dest = routing.resolve_report_chat(db, cfg, screening["chat_id"])
    dry_run = cfg.mode.dry_run
    screening_id = screening["id"]

    # Clean auto-approvals get a one-line log note, not a full report.
    if verdict == Verdict.APPROVE and not dry_run:
        name = " ".join(filter(None, [screening["first_name"], screening["last_name"]]))
        badge = "✅ approved" if screening["source"] == "join_request" else "✅ kept (clean)"
        await _safe_send(bot, dest,
                         f"{badge}: {html.escape(name or str(screening['user_id']))}"
                         f" (id <code>{screening['user_id']}</code>)"
                         + (f" — {html.escape('; '.join(notes))}" if notes else ""))
        return

    caption = _caption(screening, verdict, signals, notes, action_taken, dry_run)
    keyboard = _kb(verdict, screening["source"], screening_id, dry_run, action_taken)

    try:
        media_ids, keyboard_msg_id = await _deliver(
            bot, dest, caption, keyboard, flagged_file_ids)
    except Exception:
        if dest == cfg.bot.admin_chat_id:
            raise
        # owner DM unreachable (blocked the bot, deleted account…) — a report
        # must never vanish silently: reroute to the operator chat.
        log.warning("report to owner %s failed — rerouting to operator chat",
                    dest, exc_info=True)
        dest = cfg.bot.admin_chat_id
        rerouted = (f"⚠️ owner unreachable for chat "
                    f"<code>{screening['chat_id']}</code> — report rerouted:\n"
                    + caption)[:1024]
        media_ids, keyboard_msg_id = await _deliver(
            bot, dest, rerouted, keyboard, flagged_file_ids)

    if keyboard_msg_id is not None:
        db.save_admin_messages(screening_id, dest, media_ids, keyboard_msg_id)


async def _deliver(bot: Bot, dest: int, caption: str, keyboard,
                   flagged_file_ids: list[str]) -> tuple[list[int], int | None]:
    media_ids: list[int] = []
    try:
        if len(flagged_file_ids) == 1:
            msg = await bot.send_photo(dest, photo=flagged_file_ids[0],
                                       caption=caption, reply_markup=keyboard)
            return [msg.message_id], msg.message_id
        if flagged_file_ids:
            album = [InputMediaPhoto(media=fid) for fid in flagged_file_ids[:10]]
            msgs = await bot.send_media_group(dest, media=album)
            media_ids = [m.message_id for m in msgs]
            msg = await bot.send_message(dest, caption, reply_markup=keyboard,
                                         reply_to_message_id=media_ids[0])
            return media_ids, msg.message_id
        msg = await bot.send_message(dest, caption, reply_markup=keyboard)
        return [], msg.message_id
    except TelegramBadRequest:
        # entity/markup rejection — never lose the report over formatting
        log.warning("report send failed with entities; retrying plain", exc_info=True)
        msg = await bot.send_message(dest, _strip_html(caption),
                                     reply_markup=keyboard, parse_mode=None)
        return media_ids, msg.message_id


async def send_alert(bot: Bot, cfg: AppConfig, text: str) -> None:
    await _safe_send(bot, cfg.bot.admin_chat_id, html.escape(text))


async def _safe_send(bot: Bot, chat_id: int, text: str, reply_markup=None):
    """A report/alert must never be silently lost: fall back to plain text."""
    try:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except TelegramBadRequest:
        try:
            return await bot.send_message(chat_id, _strip_html(text),
                                          reply_markup=reply_markup, parse_mode=None)
        except Exception:
            log.exception("admin message could not be delivered AT ALL")
            return None


def _strip_html(text: str) -> str:
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def edit_resolved(bot: Bot, db: Database, screening_id: int, outcome: str) -> None:
    """Mark the report message resolved: append the outcome, drop the buttons."""
    row = db.get_admin_messages(screening_id)
    if row is None or row["keyboard_message_id"] is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=row["admin_chat_id"], message_id=row["keyboard_message_id"],
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass  # already edited / too old — the outcome message below still lands
    try:
        await bot.send_message(
            row["admin_chat_id"], html.escape(outcome),
            reply_to_message_id=row["keyboard_message_id"],
        )
    except TelegramBadRequest:
        await bot.send_message(row["admin_chat_id"], html.escape(outcome))
