"""Boot sequence: config → db → models → self-checks → polling.

Self-check philosophy (red-team requirement): every failure mode that would
otherwise be SILENT — missing admin rights, unreachable admin chat, broken
CV models — must either stop the bot or produce a loud alert. The bot never
degrades into silently approving or declining people.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import logging
import signal
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from nazoratchi import menu, notifier, routing
from nazoratchi.config import ConfigHolder, materialize_config_from_env
from nazoratchi.db import Database
from nazoratchi.handlers import (
    callbacks,
    chat_member,
    commands,
    first_message,
    join_request,
    tenancy,
)
from nazoratchi.logging_setup import setup as setup_logging
from nazoratchi.middleware import RetryAfterMiddleware
from nazoratchi.screening.gemini_check import GeminiChecker
from nazoratchi.screening.nudenet_runtime import NudeNetRuntime
from nazoratchi.screening.orchestrator import Orchestrator
from nazoratchi.strings import t

log = logging.getLogger("nazoratchi.main")

ALLOWED_UPDATES = ["chat_join_request", "chat_member", "my_chat_member",
                   "callback_query", "message"]


async def run(config_path: str) -> int:
    holder = ConfigHolder(config_path)
    cfg = holder.current
    setup_logging(cfg.logging.dir, cfg.logging.level)
    log.info("NazoratchiAI starting (dry_run=%s)", cfg.mode.dry_run)

    db = Database(cfg.db_path)

    # --- CV models: refuse to serve if unhealthy (self-check 3) ---
    try:
        runtime = NudeNetRuntime(cfg.nudenet)
    except Exception:
        log.critical("NudeNet failed to load — refusing to start", exc_info=True)
        db.close()
        return 1
    if not runtime.health_check():
        log.critical("NudeNet health check (test inference) failed — refusing to start")
        db.close()
        return 1
    if cfg.nudenet.classifier_enabled and not runtime.classifier_active:
        log.warning("running WITHOUT the v2 classifier second opinion")

    gemini = GeminiChecker(cfg.gemini)

    # link previews off globally: invite links in outcome messages would
    # otherwise unfurl into huge "VIEW GROUP" cards in the admin's DM
    bot = Bot(token=cfg.bot.token,
              default=DefaultBotProperties(parse_mode="HTML",
                                           link_preview_is_disabled=True))
    bot.session.middleware(RetryAfterMiddleware())
    orchestrator = Orchestrator(bot, db, holder, runtime, gemini)

    try:
        # seed groups from config: auto-enabled, reports go to the operator chat
        for chat_id in holder.current.chats.allowed:
            db.enable_group(chat_id, owner_user_id=None, is_seed=True)
            routing.invalidate(chat_id)

        dp = Dispatcher()
        dp.include_router(join_request.build_router(holder, db, orchestrator))
        dp.include_router(chat_member.build_router(holder, db, orchestrator))
        dp.include_router(callbacks.build_router(holder, db))
        dp.include_router(tenancy.build_router(holder, db))
        dp.include_router(commands.build_router(holder, db))
        # last: catch-all group-message handler must not shadow the commands
        dp.include_router(first_message.build_router(holder, db, orchestrator))

        # command menu + profile texts: cosmetic, must never block startup
        try:
            await menu.setup_bot_commands(bot, holder.current.default_language)
        except Exception:
            log.error("failed to register the command menu", exc_info=True)

        # SIGHUP → hot-reload thresholds/keywords/allowlists (unix only)
        if hasattr(signal, "SIGHUP"):
            with contextlib.suppress(NotImplementedError):
                asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, holder.reload)

        problems = await startup_checks(bot, holder, runtime, db)
        if problems:
            for p in problems:
                log.error("SELF-CHECK: %s", p)
            # admin chat unreachable is fatal: rejections would be invisible
            if any("admin chat" in p for p in problems):
                log.critical("admin chat unreachable — refusing to start")
                return 1
            with contextlib.suppress(Exception):
                await notifier.send_alert(
                    bot, holder.current,
                    "⚠️ NazoratchiAI started WITH PROBLEMS:\n- " + "\n- ".join(problems))

        resumed = orchestrator.resume_pending()
        if resumed:
            log.info("resumed %d unresolved screenings from previous run", resumed)
        await orchestrator.start()
        heartbeat = asyncio.create_task(
            _touch_heartbeat(Path(cfg.db_path).parent / "heartbeat"))

        with contextlib.suppress(Exception):
            await notifier.send_alert(
                bot, holder.current,
                "🟢 NazoratchiAI online\n"
                f" • Mode: {'DRY RUN' if holder.current.mode.dry_run else 'live'}\n"
                f" • Classifier: {'on' if runtime.classifier_active else 'OFF'}\n"
                f" • Resumed cases: {resumed}")

        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
        return 0
    finally:
        if "heartbeat" in locals():
            heartbeat.cancel()
        await orchestrator.stop()
        await bot.session.close()
        db.close()


async def _touch_heartbeat(path: Path, interval_s: float = 30.0) -> None:
    """Liveness signal for the Docker healthcheck: prove the event loop runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        path.touch()
        await asyncio.sleep(interval_s)


async def startup_checks(bot: Bot, holder: ConfigHolder, runtime: NudeNetRuntime,
                         db: Database) -> list[str]:
    """Returns operator-level problems (empty = all green). Per-group rights
    problems in OWNED groups are DM'd to that group's owner and only fall back
    to the operator list if the owner is unreachable — never fatal."""
    cfg = holder.current
    problems: list[str] = []

    me = await bot.get_me()  # fails loudly on a bad token — that's intended
    log.info("bot: @%s (%s)", me.username, me.id)

    for group in db.enabled_groups():
        owner = group["owner_user_id"]
        owned = bool(owner) and not group["is_seed"]
        lang = group["language"] if owned else "en"
        group_problems = await routing.check_group_rights(bot, group["chat_id"], lang)
        if not group_problems:
            continue
        if owned:
            try:
                title = html.escape(str(group["title"] or group["chat_id"]))
                await bot.send_message(
                    owner, t(lang, "main.problems_in", title=title)
                    + "\n- " + "\n- ".join(group_problems))
                continue
            except Exception:
                pass  # owner unreachable → surface to the operator below
        problems.extend(group_problems)

    # operator chat must be reachable — otherwise rejections are invisible
    try:
        msg = await bot.send_message(cfg.bot.admin_chat_id, "nazoratchi self-check")
        await bot.delete_message(cfg.bot.admin_chat_id, msg.message_id)
    except Exception as e:
        problems.append(f"admin chat {cfg.bot.admin_chat_id} unreachable: {e}")

    if cfg.gemini.enabled and not cfg.gemini.api_key:
        problems.append(f"Gemini enabled but {cfg.gemini.api_key_env} is not set "
                        f"(text screening will be regex-only)")

    if cfg.nudenet.classifier_enabled and not runtime.classifier_active:
        problems.append("v2 classifier model missing — detector-only mode "
                        "(weaker recall, no anime coverage)")

    return problems


def main() -> None:
    load_dotenv()  # .env for bare-metal/dev; real environment always wins
    parser = argparse.ArgumentParser(description="NazoratchiAI - Telegram group gatekeeper bot")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    args = parser.parse_args()
    # PaaS deployments (Railway etc.) deliver the config via env var
    materialize_config_from_env(args.config)
    try:
        sys.exit(asyncio.run(run(args.config)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
