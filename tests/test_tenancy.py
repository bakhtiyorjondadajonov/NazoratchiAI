"""/enable and /disable self-serve flows (plan §C)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nazoratchi import routing
from nazoratchi.db import Database
from nazoratchi.handlers import tenancy

from .conftest import make_config


class StubHolder:
    def __init__(self, cfg):
        self.current = cfg


def make_msg(chat_id=-100, user_id=42, member_status="administrator",
             dm_fails=False):
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(
                status=member_status, can_restrict_members=True,
                can_invite_users=True)),
        send_message=AsyncMock(
            side_effect=Exception("blocked") if dm_fails else None),
        me=AsyncMock(return_value=SimpleNamespace(id=1, username="gk_bot")),
    )
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup", title="Test Group"),
        from_user=SimpleNamespace(id=user_id, is_bot=False),
        bot=bot, reply=AsyncMock(),
    )


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    router = tenancy.build_router(StubHolder(make_config()), db)
    enable = router.message.handlers[0].callback
    disable = router.message.handlers[1].callback
    yield db, enable, disable
    db.close()


@pytest.mark.asyncio
async def test_enable_by_group_admin(env):
    db, enable, _ = env
    await enable(make_msg())
    group = db.get_group(-100)
    assert group["enabled"] == 1 and group["owner_user_id"] == 42
    assert routing.is_group_enabled(db, -100)


@pytest.mark.asyncio
async def test_enable_refused_for_non_admin(env):
    db, enable, _ = env
    await enable(make_msg(member_status="member"))
    assert db.get_group(-100) is None


@pytest.mark.asyncio
async def test_enable_aborts_when_dm_unreachable(env):
    db, enable, _ = env
    msg = make_msg(dm_fails=True)
    await enable(msg)
    assert db.get_group(-100) is None  # NOT enabled
    reply_text = msg.reply.call_args[0][0]
    assert "Start" in reply_text  # instructions to open the bot DM


@pytest.mark.asyncio
async def test_owner_cap_enforced_operator_exempt(env):
    db, enable, _ = env
    cfg = make_config()
    cfg.tenancy.max_groups_per_owner = 2
    router = tenancy.build_router(StubHolder(cfg), db)
    enable = router.message.handlers[0].callback

    for chat_id in (-1, -2):
        db.enable_group(chat_id, owner_user_id=42)
    msg = make_msg(chat_id=-3, user_id=42)
    await enable(msg)
    assert db.get_group(-3) is None  # capped
    assert "limit" in msg.reply.call_args[0][0]

    # operator (id 999 in make_config) is exempt
    await enable(make_msg(chat_id=-4, user_id=999))
    assert db.get_group(-4)["enabled"] == 1


@pytest.mark.asyncio
async def test_disable(env):
    db, enable, disable = env
    await enable(make_msg())
    assert routing.is_group_enabled(db, -100)
    await disable(make_msg())
    assert not routing.is_group_enabled(db, -100)


def test_report_routing(tmp_path):
    db = Database(tmp_path / "r.db")
    cfg = make_config()
    # unknown group → operator chat
    assert routing.resolve_report_chat(db, cfg, -1) == -200
    # seed group → operator chat
    db.enable_group(-2, owner_user_id=None, is_seed=True)
    assert routing.resolve_report_chat(db, cfg, -2) == -200
    # owned group → owner DM
    db.enable_group(-3, owner_user_id=555)
    routing.invalidate(-3)
    assert routing.resolve_report_chat(db, cfg, -3) == 555
    db.close()
