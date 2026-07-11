"""Per-group language: db storage, routing resolution, chooser at /enable,
the lang: callback (auth + onboarding), and /language."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nazoratchi import routing
from nazoratchi.db import Database
from nazoratchi.handlers import tenancy

from .conftest import make_config
from .test_tenancy import StubHolder, make_msg


# -- db + routing ------------------------------------------------------------

def test_language_roundtrip_and_default(tmp_path):
    db = Database(tmp_path / "l.db")
    assert db.group_language(-1) == "en"  # unknown group
    db.enable_group(-1, owner_user_id=42)
    assert db.group_language(-1) == "en"  # fresh row inherits the default
    db.set_group_language(-1, "uz")
    assert db.group_language(-1) == "uz"
    # re-enable must NOT reset the choice
    db.enable_group(-1, owner_user_id=42)
    assert db.group_language(-1) == "uz"
    db.close()


def test_resolve_language(tmp_path):
    db = Database(tmp_path / "r.db")
    cfg = make_config()
    assert routing.resolve_language(db, cfg, -1) == "en"  # unknown → default
    db.enable_group(-2, owner_user_id=42)
    db.set_group_language(-2, "uz")
    routing.invalidate(-2)
    assert routing.resolve_language(db, cfg, -2) == "uz"
    # cached until invalidated
    db.set_group_language(-2, "en")
    assert routing.resolve_language(db, cfg, -2) == "uz"
    routing.invalidate(-2)
    assert routing.resolve_language(db, cfg, -2) == "en"
    db.close()


def test_config_rejects_unknown_default_language():
    with pytest.raises(Exception):
        make_config(default_language="fr")
    assert make_config(default_language="uz").default_language == "uz"


# -- tenancy handlers ----------------------------------------------------------

def handlers(db, cfg=None):
    router = tenancy.build_router(StubHolder(cfg or make_config()), db)
    msg_handlers = [h.callback for h in router.message.handlers]
    lang_cb = router.callback_query.handlers[0].callback
    return msg_handlers, lang_cb


def make_cb(data, chat_type="private", user_id=42):
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        set_my_commands=AsyncMock(),
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="administrator")),
    )
    return SimpleNamespace(
        data=data, bot=bot,
        from_user=SimpleNamespace(id=user_id, username="admin_u"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=42 if chat_type == "private" else -100,
                                 type=chat_type),
            edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_enable_sends_language_chooser_to_dm(tmp_path):
    db = Database(tmp_path / "t.db")
    (enable, *_), _ = handlers(db)
    # approved group (past the operator gate) — its admin re-runs /enable
    db.enable_group(-100, owner_user_id=42, title="Test Group")
    msg = make_msg()
    await enable(msg)
    # DM probe + chooser, both to the sender's DM
    dm_texts = [c.args for c in msg.bot.send_message.call_args_list]
    assert len(dm_texts) == 2 and all(c.args[0] == 42 for c in
                                      msg.bot.send_message.call_args_list)
    chooser = msg.bot.send_message.call_args_list[1]
    assert "Tilni tanlang" in chooser.args[1]
    kb = chooser.kwargs["reply_markup"]
    assert [b.callback_data for b in kb.inline_keyboard[0]] == \
        ["lang:en:-100", "lang:uz:-100"]
    db.close()


@pytest.mark.asyncio
async def test_lang_callback_sets_language_and_sends_onboarding(tmp_path):
    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=42, title="G")
    _, lang_cb = handlers(db)

    cb = make_cb("lang:uz:-100")
    await lang_cb(cb)
    assert db.group_language(-100) == "uz"
    cb.message.edit_text.assert_awaited_once()
    assert "til" in cb.message.edit_text.call_args.args[0]  # uz confirmation
    onboarding = cb.bot.send_message.call_args.args[1]
    assert "NazoratchiAI qanday ishlaydi" in onboarding
    db.close()


@pytest.mark.asyncio
async def test_lang_callback_group_pick_skips_onboarding(tmp_path):
    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=42, title="G")
    _, lang_cb = handlers(db)

    cb = make_cb("lang:en:-100", chat_type="supergroup")
    await lang_cb(cb)
    assert db.group_language(-100) == "en"
    cb.message.edit_text.assert_awaited_once()
    cb.bot.send_message.assert_not_awaited()  # no guide spam in the group
    db.close()


@pytest.mark.asyncio
async def test_lang_callback_rejects_unauthorized_and_malformed(tmp_path):
    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=42, title="G")
    _, lang_cb = handlers(db)

    # stranger: not operator, not owner, not group admin
    cb = make_cb("lang:uz:-100", user_id=777)
    cb.bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status="member"))
    await lang_cb(cb)
    assert db.group_language(-100) == "en"  # unchanged
    assert cb.answer.call_args.kwargs.get("show_alert") is True

    # malformed / unknown language
    for data in ("lang:xx:-100", "lang:uz:notanint"):
        cb = make_cb(data)
        await lang_cb(cb)
        assert db.group_language(-100) == "en"
    db.close()


@pytest.mark.asyncio
async def test_lang_pick_updates_command_menus(tmp_path):
    from aiogram.types import BotCommandScopeChat, BotCommandScopeChatAdministrators

    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=42, title="G")
    _, lang_cb = handlers(db)

    # DM pick → group-admin menu override + the picker's DM menu
    cb = make_cb("lang:uz:-100")
    await lang_cb(cb)
    scopes = [c.kwargs["scope"] for c in cb.bot.set_my_commands.call_args_list]
    assert isinstance(scopes[0], BotCommandScopeChatAdministrators)
    assert scopes[0].chat_id == -100
    assert isinstance(scopes[1], BotCommandScopeChat) and scopes[1].chat_id == 42

    # group pick → only the group-admin override
    cb = make_cb("lang:en:-100", chat_type="supergroup")
    await lang_cb(cb)
    scopes = [c.kwargs["scope"] for c in cb.bot.set_my_commands.call_args_list]
    assert len(scopes) == 1
    assert isinstance(scopes[0], BotCommandScopeChatAdministrators)
    db.close()


# -- /blocked //held language (regression: operator got English in a uz group) --

def make_list_msg(db, chat_id=-100, chat_type="supergroup", user_id=999):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id),
        bot=SimpleNamespace(get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="administrator"))),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_blocked_empty_text_follows_the_chat_language(tmp_path):
    from nazoratchi.handlers import commands
    from nazoratchi.strings import t

    db = Database(tmp_path / "c.db")
    db.enable_group(-100, owner_user_id=42, title="G")
    db.set_group_language(-100, "uz")
    router = commands.build_router(StubHolder(make_config()), db)
    cmd_blocked = router.message.handlers[0].callback

    # operator (999) INSIDE the uz group → Uzbek, despite the operator scope
    msg = make_list_msg(db)
    await cmd_blocked(msg)
    assert msg.answer.call_args.args[0] == t("uz", "list.blocked_empty")

    # operator in their DM, owning nothing → English default
    msg = make_list_msg(db, chat_id=999, chat_type="private")
    await cmd_blocked(msg)
    assert msg.answer.call_args.args[0] == t("en", "list.blocked_empty")

    # owner of exactly one uz group in their DM → Uzbek
    msg = make_list_msg(db, chat_id=42, chat_type="private", user_id=42)
    await cmd_blocked(msg)
    assert msg.answer.call_args.args[0] == t("uz", "list.blocked_empty")
    db.close()


@pytest.mark.asyncio
async def test_language_command_in_dm_lists_owned_groups(tmp_path):
    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=42, title="Guruh A")
    router = tenancy.build_router(StubHolder(make_config()), db)
    cmd_language_private = router.message.handlers[3].callback

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42), answer=AsyncMock())
    await cmd_language_private(msg)
    assert "Guruh A" in msg.answer.call_args.args[0]
    assert msg.answer.call_args.kwargs["reply_markup"] is not None

    # no owned groups → pointer to /enable
    msg2 = SimpleNamespace(
        chat=SimpleNamespace(id=7, type="private"),
        from_user=SimpleNamespace(id=7), answer=AsyncMock())
    await cmd_language_private(msg2)
    assert "/enable" in msg2.answer.call_args.args[0]
    db.close()
