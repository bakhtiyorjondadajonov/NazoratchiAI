"""Admin reports: verdict caption + action buttons. TEXT-ONLY by policy —
flagged (often explicit) photos are never forwarded to the admin's DM; the
report describes the evidence and the clickable profile link lets the admin
look for themselves.

Telegram constraints honored here:
- captions are kept ≤1024 chars, assembled line-by-line;
- all user-supplied text is HTML-escaped; if Telegram still rejects the
  entities the send is retried without parse_mode (a report must never be
  silently lost).
"""

from __future__ import annotations

import html
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from nazoratchi.config import AppConfig
from nazoratchi.db import Database
from nazoratchi.screening.verdict import Signal, SignalKind, Verdict
from nazoratchi.strings import label, t

log = logging.getLogger(__name__)


def user_link(user_id: int, first_name: str | None, last_name: str | None,
              username: str | None) -> str:
    """Tappable profile link for the reported account. A public @username link
    always opens; the tg://user fallback resolves when the viewer's client can
    look the user up — the <code>id</code> shown next to it stays copyable."""
    name = html.escape(
        (" ".join(filter(None, [first_name, last_name])) or str(user_id))[:64])
    if username:
        return f'<a href="https://t.me/{username}">{name}</a>'
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def humanize_signal(lang: str, s: Signal) -> str:
    """One finding as a localized human sentence with a percentage. Renders
    from the signal's structure; ANY parsing problem falls back to the raw
    detail — a report must never lose evidence over formatting."""
    try:
        extra = s.extra or {}
        pct = round(s.score * 100) if s.score is not None else None
        if (s.kind in (SignalKind.EXPOSED_HIT, SignalKind.COVERED_HIT)
                and extra.get("class") and pct is not None):
            what = label(lang, "class", extra["class"])
            if extra.get("origin") == "message":
                return t(lang, "ev.msg_photo", what=what, pct=pct)
            return t(lang, "ev.profile_photo",
                     n=(s.photo_index or 0) + 1, what=what, pct=pct)
        if s.kind == SignalKind.BELLY_COMBO_HIT and extra.get("class"):
            return t(lang, "ev.belly", what=label(lang, "class", extra["class"]))
        if s.kind == SignalKind.CLASSIFIER_UNSAFE and pct is not None:
            return t(lang, "ev.classifier", pct=pct)
        if s.kind in (SignalKind.GEMINI_ADULT, SignalKind.GEMINI_BLOCKED):
            reason = s.detail.split(":", 1)[1].strip() if ":" in s.detail else s.detail
            return t(lang, "ev.gemini", reason=html.escape(reason[:100]))
        if s.kind in (SignalKind.TEXT_HARD, SignalKind.TEXT_SOFT):
            return _humanize_text_hit(lang, s.detail)
        if s.kind == SignalKind.PHOTO_FETCH_FAILED:
            return t(lang, "ev.fetch_failed")
        if s.kind == SignalKind.INFRA_ERROR:
            return t(lang, "ev.infra")
    except Exception:  # noqa: BLE001 — display-only path, never break a report
        pass
    return html.escape(s.detail[:120])


def _humanize_text_hit(lang: str, detail: str) -> str:
    """Text-checker details follow '{field}: <pattern>' — see text_check.py."""
    fld, sep, rest = detail.partition(": ")
    if not sep:
        raise ValueError(detail)
    field = label(lang, "field", fld)
    for pattern, key, arg in (
        (r"keyword '(.+)'$", "ev.keyword", "word"),
        (r"obfuscated '(.+)'$", "ev.obfuscated", "word"),
        (r"emoji combo (.+)$", "ev.emoji_combo", "emojis"),
        (r"emoji (.+)$", "ev.emoji", "emoji"),
        (r"link pattern '(.+)'$", "ev.link", "pattern"),
        (r"(\d+) invisible chars$", "ev.invisible", "n"),
    ):
        m = re.match(pattern, rest)
        if m:
            return t(lang, key, field=field,
                     **{arg: html.escape(m.group(1)[:60])})
    if rest == "mention + signal":
        return t(lang, "ev.mention", field=field)
    raise ValueError(detail)  # unknown shape → caller falls back to raw


def humanize_note(lang: str, note: str) -> str:
    """Notes arrive as tokens (e.g. 'rescreen_bio_read'); legacy/free-text
    notes pass through escaped."""
    resolved = label(lang, "note", note)
    return resolved if resolved != note else html.escape(note[:150])


def _kb(verdict: Verdict, source: str, screening_id: int, dry_run: bool,
        action_taken: str = "", lang: str = "en") -> InlineKeyboardMarkup | None:
    """Callback data format: gk:<action>:<screening_id> (well under 64 bytes)."""
    def btn(key: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=t(lang, key),
                                    callback_data=f"gk:{action}:{screening_id}")

    if verdict == Verdict.APPROVE and not dry_run:
        return None
    if source == "join_request":
        if dry_run or verdict == Verdict.HOLD:
            # request is still pending (dry-run never touched it) — both work
            return InlineKeyboardMarkup(inline_keyboard=[[
                btn("btn.approve", "approve"), btn("btn.decline", "decline"),
            ]])
        if verdict == Verdict.DECLINE:
            return InlineKeyboardMarkup(inline_keyboard=[[
                btn("btn.override", "override"),
            ]])
    else:  # open-join path: there is no join request to approve/decline
        if action_taken in ("banned", "banned_pending"):
            return InlineKeyboardMarkup(inline_keyboard=[[
                btn("btn.unban", "unban"),
            ]])
        if dry_run or verdict in (Verdict.HOLD, Verdict.DECLINE):
            # user is still inside (dry-run, infra-failure hold, or ban failed)
            return InlineKeyboardMarkup(inline_keyboard=[[
                btn("btn.ban", "kick"), btn("btn.keep", "keep"),
            ]])
    return None


def _caption(screening, verdict: Verdict, signals: list[Signal],
             notes: list[str], action_taken: str, dry_run: bool,
             lang: str = "en", chat_label=None) -> str:
    """Every line is a self-contained HTML fragment, and the caption is
    assembled line-by-line within the 1024-char budget — a blind slice could
    cut inside a tag or entity and make Telegram reject the whole message."""
    e = html.escape
    link = user_link(screening["user_id"], screening["first_name"],
                     screening["last_name"], screening["username"])
    # the id sits on its own LTR line: an RTL display name in a mixed line
    # visually scrambles everything after it
    id_line = f"🆔 <code>{screening['user_id']}</code>" + (
        f" · @{e(screening['username'])}" if screening["username"] else "")
    chat = e(str(chat_label if chat_label is not None else screening["chat_id"])[:40])
    lines = [
        f"{t(lang, 'report.dry_run') if dry_run else ''}"
        f"{t(lang, f'verdict.{verdict.value}')}",
        "",
        f"👤 {link}",
        id_line,
        t(lang, "report.chat", chat=chat),
        t(lang, "report.source", source=label(lang, "source", screening["source"])),
        t(lang, "report.action", action=e(label(lang, "action", action_taken))),
    ]
    triggers = [s for s in signals if s.kind not in
                (SignalKind.TEXT_SOFT, SignalKind.NO_PHOTO, SignalKind.GEMINI_UNAVAILABLE)]
    if triggers:
        lines += ["", t(lang, "report.triggered")]
        lines += [f" • {humanize_signal(lang, s)}" for s in triggers[:8]]
        if len(triggers) > 8:
            lines.append(t(lang, "report.more_triggers", n=len(triggers) - 8))
    soft = [s for s in signals if s.kind == SignalKind.TEXT_SOFT]
    if soft:
        lines += ["", t(lang, "report.soft")
                  + "; ".join(humanize_signal(lang, s) for s in soft[:4])]
    if screening["bio"]:
        lines += ["", f"{t(lang, 'report.bio')} <pre>{e(screening['bio'][:120])}</pre>"]
    for note in notes:
        lines.append(f"ℹ️ {humanize_note(lang, note)}")

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
    from nazoratchi import routing  # local import: routing imports config, not notifier

    dest = routing.resolve_report_chat(db, cfg, screening["chat_id"])
    lang = routing.resolve_language(db, cfg, screening["chat_id"])
    dry_run = cfg.mode.dry_run
    screening_id = screening["id"]

    # Clean auto-approvals get a one-line log note, not a full report.
    if verdict == Verdict.APPROVE and not dry_run:
        link = user_link(screening["user_id"], screening["first_name"],
                         screening["last_name"], screening["username"])
        badge = t(lang, "report.approved_clean" if screening["source"] == "join_request"
                  else "report.kept_clean")
        await _safe_send(bot, dest,
                         f"{badge} — {link}\n"
                         f"🆔 <code>{screening['user_id']}</code>"
                         + (f" · ℹ️ {'; '.join(humanize_note(lang, n) for n in notes)}"
                            if notes else ""))
        return

    group = db.get_group(screening["chat_id"])
    chat_label = group["title"] if group and group["title"] else None
    caption = _caption(screening, verdict, signals, notes, action_taken, dry_run,
                       lang, chat_label)
    keyboard = _kb(verdict, screening["source"], screening_id, dry_run,
                   action_taken, lang)

    try:
        keyboard_msg_id = await _deliver(bot, dest, caption, keyboard)
    except Exception:
        if dest == cfg.bot.admin_chat_id:
            raise
        # owner DM unreachable (blocked the bot, deleted account…) — a report
        # must never vanish silently: reroute to the operator chat.
        log.warning("report to owner %s failed — rerouting to operator chat",
                    dest, exc_info=True)
        dest = cfg.bot.admin_chat_id
        # operator-facing → always English
        rerouted = (t("en", "report.rerouted", chat_id=screening["chat_id"])
                    + "\n" + caption)[:1024]
        keyboard_msg_id = await _deliver(bot, dest, rerouted, keyboard)

    if keyboard_msg_id is not None:
        db.save_admin_messages(screening_id, dest, [], keyboard_msg_id)


async def _deliver(bot: Bot, dest: int, caption: str, keyboard) -> int | None:
    """Text-only by policy — the flagged photos themselves are never sent."""
    try:
        msg = await bot.send_message(dest, caption, reply_markup=keyboard)
        return msg.message_id
    except TelegramBadRequest:
        # entity/markup rejection — never lose the report over formatting
        log.warning("report send failed with entities; retrying plain", exc_info=True)
        msg = await bot.send_message(dest, _strip_html(caption),
                                     reply_markup=keyboard, parse_mode=None)
        return msg.message_id


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
