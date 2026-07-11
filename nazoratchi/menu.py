"""Telegram command menu + bot profile texts, per-locale and per-scope.

Two layers:
- setup_bot_commands() runs once at boot and registers the GLOBAL menus:
  private chats and group *administrators* only (no default scope, so regular
  group members see no menu at all). Telegram picks the locale variant from
  each user's app language, with the no-language_code call as the fallback.
- set_group_menu()/set_private_menu() register PER-CHAT overrides after a
  /language pick — per-chat scopes beat the global ones, so the group's menu
  follows the group's chosen language regardless of each admin's app language.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
)

from nazoratchi.strings import LANGS, t

PRIVATE_COMMANDS = ("blocked", "held", "language")
GROUP_ADMIN_COMMANDS = ("enable", "disable", "language", "blocked", "held")


def _commands(keys: tuple[str, ...], lang: str) -> list[BotCommand]:
    return [BotCommand(command=k, description=t(lang, f"cmd.{k}")) for k in keys]


async def setup_bot_commands(bot: Bot, default_lang: str) -> None:
    """Global registration at boot — idempotent, re-run on every start."""
    for scope, keys in (
        (BotCommandScopeAllPrivateChats(), PRIVATE_COMMANDS),
        (BotCommandScopeAllChatAdministrators(), GROUP_ADMIN_COMMANDS),
    ):
        # fallback for app languages we don't cover, then explicit variants
        await bot.set_my_commands(_commands(keys, default_lang), scope=scope)
        for lang in LANGS:
            await bot.set_my_commands(_commands(keys, lang), scope=scope,
                                      language_code=lang)

    await bot.set_my_description(t(default_lang, "bot.description"))
    await bot.set_my_short_description(t(default_lang, "bot.short_description"))
    for lang in LANGS:
        await bot.set_my_description(t(lang, "bot.description"),
                                     language_code=lang)
        await bot.set_my_short_description(t(lang, "bot.short_description"),
                                           language_code=lang)


async def set_group_menu(bot: Bot, chat_id: int, lang: str) -> None:
    """Pin the group admins' menu to the group's chosen language."""
    await bot.set_my_commands(
        _commands(GROUP_ADMIN_COMMANDS, lang),
        scope=BotCommandScopeChatAdministrators(chat_id=chat_id))


async def set_private_menu(bot: Bot, chat_id: int, lang: str) -> None:
    """Pin one DM's menu to the language its owner just picked."""
    await bot.set_my_commands(
        _commands(PRIVATE_COMMANDS, lang),
        scope=BotCommandScopeChat(chat_id=chat_id))
