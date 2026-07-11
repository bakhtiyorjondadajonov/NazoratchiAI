"""Localized report captions/buttons and clickable profile links."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nazoratchi import notifier
from nazoratchi.db import Database
from nazoratchi.notifier import _caption, _kb, user_link
from nazoratchi.screening.verdict import Signal, SignalKind, Verdict

from .conftest import make_config
from .test_notifier import assert_valid_caption, screening_row


# -- user_link ---------------------------------------------------------------

def test_user_link_public_username():
    link = user_link(42, "Ann", None, "ann_channel")
    assert link == '<a href="https://t.me/ann_channel">Ann</a>'


def test_user_link_no_username_uses_mention_scheme():
    assert user_link(42, "Ann", "Lee", None) == '<a href="tg://user?id=42">Ann Lee</a>'


def test_user_link_escapes_hostile_name():
    link = user_link(42, '<b>&"', None, None)
    assert "<b>" not in link and "&amp;" in link


def test_user_link_falls_back_to_id():
    assert ">42<" in user_link(42, None, None, None)


# -- captions ----------------------------------------------------------------

def caption_for(lang, **over):
    return _caption(screening_row(**over), Verdict.DECLINE,
                    [Signal(SignalKind.TEXT_HARD, 'message: keyword "porno"')],
                    notes=[], action_taken="banned", dry_run=False,
                    lang=lang, chat_label="My Group")


def test_caption_english():
    caption = caption_for("en")
    assert_valid_caption(caption)
    assert "DECLINED" in caption and "Triggered" in caption
    assert "My Group" in caption
    assert '<a href="https://t.me/test_user">' in caption
    assert "<code>42</code>" in caption  # copyable id stays


def test_caption_uzbek():
    caption = caption_for("uz")
    assert_valid_caption(caption)
    assert "RAD ETILDI" in caption and "Aniqlangan belgilar" in caption
    assert "DECLINED" not in caption


def test_caption_link_without_username():
    caption = caption_for("en", username=None)
    assert '<a href="tg://user?id=42">' in caption


def test_caption_dry_run_prefix_localized():
    row = screening_row()
    caption = _caption(row, Verdict.HOLD, [], [], "pending", dry_run=True, lang="uz")
    assert "SINOV REJIMI" in caption and "KUTILMOQDA" in caption


# -- keyboards ---------------------------------------------------------------

def test_kb_labels_localized_callback_data_unchanged():
    kb = _kb(Verdict.HOLD, "join_request", 7, False, "pending", lang="uz")
    texts = [b.text for row in kb.inline_keyboard for b in row]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert texts == ["✅ Tasdiqlash", "⛔ Rad etish"]
    assert datas == ["gk:approve:7", "gk:decline:7"]


# -- report() end-to-end -----------------------------------------------------

@pytest.mark.asyncio
async def test_report_uses_group_language_and_linked_one_liner(tmp_path):
    db = Database(tmp_path / "i.db")
    cfg = make_config()
    db.enable_group(-100, owner_user_id=555, title="Guruh")
    db.set_group_language(-100, "uz")

    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
    sid = db.create_screening(chat_id=-100, user_id=42, source="chat_member",
                              user_chat_id=42, bio=None, first_name="Foo",
                              last_name=None, username="foo_u")
    row = db.get_screening(sid)

    # full report in the group's language
    await notifier.report(
        bot=bot, cfg=cfg, db=db, screening=row, verdict=Verdict.DECLINE,
        signals=[Signal(SignalKind.TEXT_HARD, "kw")], flagged_file_ids=[],
        notes=[], action_taken="banned")
    text = bot.send_message.call_args[0][1]
    kb = bot.send_message.call_args.kwargs["reply_markup"]
    assert "RAD ETILDI" in text and "Guruh" in text
    assert '<a href="https://t.me/foo_u">' in text
    assert kb.inline_keyboard[0][0].text == "🔓 Blokdan chiqarish"

    # clean approval one-liner: localized + clickable
    bot.send_message.reset_mock()
    await notifier.report(
        bot=bot, cfg=cfg, db=db, screening=row, verdict=Verdict.APPROVE,
        signals=[], flagged_file_ids=[], notes=[], action_taken="kept")
    one_liner = bot.send_message.call_args[0][1]
    assert "qoldirildi (toza)" in one_liner
    assert '<a href="https://t.me/foo_u">' in one_liner
    db.close()
