"""Caption budget, keyboard matrix, report routing, and ban semantics."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nazoratchi import notifier
from nazoratchi.db import Database
from nazoratchi.notifier import _caption, _kb
from nazoratchi.screening.orchestrator import Orchestrator
from nazoratchi.screening.verdict import Signal, SignalKind, Verdict

from .conftest import make_config


def screening_row(**over):
    row = {
        "id": 7, "chat_id": -100, "user_id": 42, "source": "join_request",
        "user_chat_id": 42, "bio": None, "first_name": "Test", "last_name": None,
        "username": "test_user", "status": "resolved", "created_at": 0.0,
    }
    row.update(over)
    return row


def assert_valid_caption(caption: str):
    assert len(caption) <= 1024
    # every opened tag is closed (lines are self-contained fragments)
    for tag in ("b", "i", "code", "pre"):
        assert caption.count(f"<{tag}>") == caption.count(f"</{tag}>"), tag
    # no bare & (all entities escaped)
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|#)", caption)


def test_caption_budget_with_hostile_bio():
    # escape-inflating bio (& < >) plus many triggers — the worst case
    row = screening_row(bio="&<>\"" * 60, first_name="<b>&" * 20)
    signals = [Signal(SignalKind.COVERED_HIT, f"FEMALE_BREAST_COVERED=0.{50+i}",
                      score=0.5, photo_index=i) for i in range(20)]
    signals += [Signal(SignalKind.TEXT_SOFT, "bio: keyword '&&&'" * 10)]
    caption = _caption(row, Verdict.HOLD, signals,
                       notes=["Gemini unavailable — text verdict is regex-only"],
                       action_taken="pending", dry_run=False)
    assert_valid_caption(caption)
    assert "HELD FOR REVIEW" in caption


def test_caption_simple_report():
    row = screening_row(bio="hello")
    caption = _caption(row, Verdict.DECLINE,
                       [Signal(SignalKind.EXPOSED_HIT, "FEMALE_BREAST_EXPOSED=0.41")],
                       notes=[], action_taken="declined", dry_run=False)
    assert_valid_caption(caption)
    assert "DECLINED" in caption and "FEMALE_BREAST_EXPOSED" in caption


def buttons(kb):
    return [b.callback_data.split(":")[1] for row in kb.inline_keyboard for b in row]


def test_kb_matrix():
    # clean approve, live mode → no keyboard
    assert _kb(Verdict.APPROVE, "join_request", 1, dry_run=False) is None
    # hold on a pending request → approve/decline
    assert buttons(_kb(Verdict.HOLD, "join_request", 1, False, "pending")) == ["approve", "decline"]
    # auto-declined request → override only
    assert buttons(_kb(Verdict.DECLINE, "join_request", 1, False, "declined")) == ["override"]
    # open-join path: content signals ban immediately → unban is the remedy
    assert buttons(_kb(Verdict.DECLINE, "chat_member", 1, False, "banned")) == ["unban"]
    assert buttons(_kb(Verdict.HOLD, "chat_member", 1, False, "banned_pending")) == ["unban"]
    # infra-failure hold / failed ban: user still inside → manual Ban/Keep
    assert buttons(_kb(Verdict.HOLD, "chat_member", 1, False, "kept_flagged")) == ["kick", "keep"]
    assert buttons(_kb(Verdict.DECLINE, "chat_member", 1, False, "ban_failed")) == ["kick", "keep"]
    assert _kb(Verdict.APPROVE, "chat_member", 1, False, "kept") is None


@pytest.mark.asyncio
async def test_first_message_ban_revokes_messages(tmp_path):
    db = Database(tmp_path / "o.db")
    cfg = make_config()

    class Holder:
        current = cfg

    bot = SimpleNamespace(ban_chat_member=AsyncMock())
    orch = Orchestrator(bot, db, Holder(), runtime=None, gemini=None)

    row = {"chat_id": -100, "user_id": 42, "source": "first_message"}
    action = await orch._apply(row, Verdict.DECLINE, cfg, signals=[])
    assert action == "banned"
    bot.ban_chat_member.assert_awaited_once_with(
        chat_id=-100, user_id=42, revoke_messages=True)

    # join-echo path must NOT revoke (user may have history)
    bot.ban_chat_member.reset_mock()
    row["source"] = "chat_member"
    await orch._apply(row, Verdict.DECLINE, cfg, signals=[])
    bot.ban_chat_member.assert_awaited_once_with(
        chat_id=-100, user_id=42, revoke_messages=None)
    db.close()


@pytest.mark.asyncio
async def test_owner_dm_failure_reroutes_to_operator(tmp_path):
    db = Database(tmp_path / "n.db")
    cfg = make_config()  # operator chat -200
    db.enable_group(-100, owner_user_id=555, title="G")

    def send(chat_id, *a, **k):
        if chat_id == 555:
            raise RuntimeError("bot blocked by owner")
        return SimpleNamespace(message_id=10)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    sid = db.create_screening(chat_id=-100, user_id=42, source="join_request",
                              user_chat_id=42, bio=None, first_name="A",
                              last_name=None, username=None)
    row = db.get_screening(sid)
    await notifier.report(
        bot=bot, cfg=cfg, db=db, screening=row, verdict=Verdict.HOLD,
        signals=[Signal(SignalKind.COVERED_HIT, "FEMALE_BREAST_COVERED=0.5")],
        flagged_file_ids=[], notes=[], action_taken="pending")

    saved = db.get_admin_messages(sid)
    assert saved["admin_chat_id"] == -200  # rerouted, buttons still tracked
    rerouted_text = bot.send_message.call_args[0][1]
    assert "rerouted" in rerouted_text
    db.close()


def test_kb_dry_run_is_source_aware():
    # M2 regression: dry-run chat_member reports must NEVER offer join-request
    # actions (there is no request to approve) — they get kick/keep
    assert buttons(_kb(Verdict.DECLINE, "chat_member", 1, True)) == ["kick", "keep"]
    assert buttons(_kb(Verdict.HOLD, "chat_member", 1, True)) == ["kick", "keep"]
    assert buttons(_kb(Verdict.APPROVE, "chat_member", 1, True)) == ["kick", "keep"]
    # dry-run join_request keeps the pending request actionable
    assert buttons(_kb(Verdict.DECLINE, "join_request", 1, True)) == ["approve", "decline"]
    assert buttons(_kb(Verdict.APPROVE, "join_request", 1, True)) == ["approve", "decline"]
