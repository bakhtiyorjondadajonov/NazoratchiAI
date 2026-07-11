"""Command menu registration: scopes, locales, Telegram limits."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import (
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
)

from nazoratchi import menu
from nazoratchi.strings import LANGS, t


def make_bot():
    return SimpleNamespace(
        set_my_commands=AsyncMock(),
        set_my_description=AsyncMock(),
        set_my_short_description=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_setup_registers_scoped_localized_menus():
    bot = make_bot()
    await menu.setup_bot_commands(bot, "en")

    calls = bot.set_my_commands.call_args_list
    # 2 scopes × (fallback + en + uz)
    assert len(calls) == 2 * (1 + len(LANGS))
    seen = {(type(c.kwargs["scope"]).__name__, c.kwargs.get("language_code"))
            for c in calls}
    assert seen == {
        ("BotCommandScopeAllPrivateChats", None),
        ("BotCommandScopeAllPrivateChats", "en"),
        ("BotCommandScopeAllPrivateChats", "uz"),
        ("BotCommandScopeAllChatAdministrators", None),
        ("BotCommandScopeAllChatAdministrators", "en"),
        ("BotCommandScopeAllChatAdministrators", "uz"),
    }  # never BotCommandScopeDefault → members see no menu

    for c in calls:
        cmds = c.args[0]
        keys = tuple(cmd.command for cmd in cmds)
        if isinstance(c.kwargs["scope"], BotCommandScopeAllPrivateChats):
            assert keys == menu.PRIVATE_COMMANDS
        else:
            assert keys == menu.GROUP_ADMIN_COMMANDS
        lang = c.kwargs.get("language_code") or "en"
        for cmd in cmds:
            assert cmd.description == t(lang, f"cmd.{cmd.command}")

    # profile texts: default + per-lang, uz differs from en
    descs = {c.kwargs.get("language_code"): c.args[0]
             for c in bot.set_my_description.call_args_list}
    assert set(descs) == {None, "en", "uz"} and descs["en"] != descs["uz"]
    shorts = {c.kwargs.get("language_code"): c.args[0]
              for c in bot.set_my_short_description.call_args_list}
    assert set(shorts) == {None, "en", "uz"}


def test_descriptions_within_telegram_limits():
    for lang in LANGS:
        for key in menu.PRIVATE_COMMANDS + menu.GROUP_ADMIN_COMMANDS:
            assert 3 <= len(t(lang, f"cmd.{key}")) <= 256, (lang, key)
        assert len(t(lang, "bot.description")) <= 512, lang
        assert len(t(lang, "bot.short_description")) <= 120, lang


@pytest.mark.asyncio
async def test_per_chat_overrides():
    bot = make_bot()
    await menu.set_group_menu(bot, -100, "uz")
    scope = bot.set_my_commands.call_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChatAdministrators)
    assert scope.chat_id == -100
    cmds = bot.set_my_commands.call_args.args[0]
    assert cmds[0].description == t("uz", "cmd.enable")

    await menu.set_private_menu(bot, 555, "uz")
    scope = bot.set_my_commands.call_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == 555
    assert tuple(c.command for c in
                 bot.set_my_commands.call_args.args[0]) == menu.PRIVATE_COMMANDS
