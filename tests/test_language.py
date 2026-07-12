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
async def test_enable_without_pref_sends_chooser_to_dm(tmp_path):
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
        ["lang:en:all", "lang:uz:all"]
    db.close()


@pytest.mark.asyncio
async def test_enable_with_pref_inherits_it_no_chooser(tmp_path):
    """An owner who already picked at /start gets the group in their
    language immediately — no chooser DM."""
    db = Database(tmp_path / "t.db")
    (enable, *_), _ = handlers(db)
    db.enable_group(-100, owner_user_id=42, title="Test Group")
    db.set_user_language(42, "uz")
    msg = make_msg()
    await enable(msg)
    assert db.group_language(-100) == "uz"
    # only the DM probe — no chooser
    assert len(msg.bot.send_message.call_args_list) == 1
    db.close()


@pytest.mark.asyncio
async def test_start_greets_and_offers_languages(tmp_path):
    db = Database(tmp_path / "t.db")
    router = tenancy.build_router(StubHolder(make_config()), db)
    cmd_start = router.message.handlers[4].callback
    msg = SimpleNamespace(chat=SimpleNamespace(id=42, type="private"),
                          from_user=SimpleNamespace(id=42), answer=AsyncMock())
    await cmd_start(msg)
    welcome = msg.answer.call_args_list[0].args[0]
    assert "Welcome" in welcome and "xush kelibsiz" in welcome
    chooser = msg.answer.call_args_list[1]
    assert [b.callback_data for b in
            chooser.kwargs["reply_markup"].inline_keyboard[0]] == \
        ["lang:en:all", "lang:uz:all"]
    db.close()


@pytest.mark.asyncio
async def test_lang_callback_sets_language_and_sends_onboarding(tmp_path):
    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=42, title="G")
    _, lang_cb = handlers(db)

    cb = make_cb("lang:uz:all")
    await lang_cb(cb)
    assert db.user_language(42) == "uz"      # personal pick stored
    assert db.group_language(-100) == "uz"   # owned group switched
    cb.message.edit_text.assert_awaited_once()
    assert "Til" in cb.message.edit_text.call_args.args[0]  # uz confirmation
    onboarding = cb.bot.send_message.call_args.args[1]
    assert "NazoratchiAI qanday ishlaydi" in onboarding

    # second pick: guide NOT sent again, groups switch back
    cb2 = make_cb("lang:en:all")
    await lang_cb(cb2)
    assert db.group_language(-100) == "en"
    cb2.bot.send_message.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_lang_pick_with_no_groups_stores_pref(tmp_path):
    """/start case: a pick before owning any group must work."""
    db = Database(tmp_path / "t.db")
    _, lang_cb = handlers(db)
    cb = make_cb("lang:uz:all", user_id=777)
    await lang_cb(cb)
    assert db.user_language(777) == "uz"
    assert "Til" in cb.message.edit_text.call_args.args[0]
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
async def test_lang_callback_scoping_and_malformed(tmp_path):
    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=42, title="G")
    _, lang_cb = handlers(db)

    # a stranger's pick sets THEIR preference, never someone else's group
    cb = make_cb("lang:uz:all", user_id=777)
    await lang_cb(cb)
    assert db.user_language(777) == "uz"
    assert db.group_language(-100) == "en"  # not their group — unchanged

    # unknown language code is rejected
    cb = make_cb("lang:xx:all")
    await lang_cb(cb)
    assert db.user_language(42) is None
    assert cb.answer.call_args.kwargs.get("show_alert") is True

    # legacy chat-bound data from old chooser messages still works
    cb = make_cb("lang:uz:-100")
    await lang_cb(cb)
    assert db.user_language(42) == "uz" and db.group_language(-100) == "uz"
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
async def test_language_command_in_dm_shows_single_chooser(tmp_path):
    db = Database(tmp_path / "t.db")
    router = tenancy.build_router(StubHolder(make_config()), db)
    cmd_language_private = router.message.handlers[3].callback

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        from_user=SimpleNamespace(id=42), answer=AsyncMock())
    await cmd_language_private(msg)
    msg.answer.assert_awaited_once()
    assert "Tilni tanlang" in msg.answer.call_args.args[0]
    assert msg.answer.call_args.kwargs["reply_markup"] is not None
    db.close()


@pytest.mark.asyncio
async def test_language_command_in_group_redirects_to_dm(tmp_path):
    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=42, title="G")
    router = tenancy.build_router(StubHolder(make_config()), db)
    cmd_language_group = router.message.handlers[2].callback

    msg = make_msg()
    await cmd_language_group(msg)
    # chooser went to the DM, short note stayed in the group
    dm = msg.bot.send_message.call_args
    assert dm.args[0] == 42 and "Tilni tanlang" in dm.args[1]
    assert "📬" in msg.reply.call_args.args[0]
    db.close()
