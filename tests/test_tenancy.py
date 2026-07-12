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
async def test_enable_by_operator_bypasses_gate(env):
    db, enable, _ = env
    await enable(make_msg(user_id=999))  # operator id in make_config
    group = db.get_group(-100)
    assert group["enabled"] == 1 and group["owner_user_id"] == 999
    assert group["approval"] == "approved"
    assert routing.is_group_enabled(db, -100)


@pytest.mark.asyncio
async def test_enable_by_stranger_becomes_request(env):
    """Non-operator /enable does NOT enable — it asks the operator."""
    db, enable, _ = env
    msg = make_msg()  # user 42, group admin, not operator
    await enable(msg)
    group = db.get_group(-100)
    assert group["enabled"] == 0 and group["approval"] == "pending"
    assert group["owner_user_id"] == 42
    assert not routing.is_group_enabled(db, -100)
    # operator got the request with decision buttons; requester got req.sent
    dms = msg.bot.send_message.call_args_list
    op_call = next(c for c in dms if c.args[0] == -200)  # operator chat
    datas = [b.callback_data for row in
             op_call.kwargs["reply_markup"].inline_keyboard for b in row]
    assert datas == ["req:approve:-100", "req:reject:-100"]
    assert any(c.args[0] == 42 for c in dms)

    # a second /enable while pending must NOT ping the operator again
    msg2 = make_msg()
    await enable(msg2)
    assert not any(c.args and c.args[0] == -200
                   for c in msg2.bot.send_message.call_args_list)
    assert "⏳" in msg2.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_enable_after_approval_needs_no_new_request(env):
    """Approved group: /disable then /enable again goes straight through."""
    db, enable, disable = env
    db.enable_group(-100, owner_user_id=42, title="Test Group")  # post-gate state
    await disable(make_msg())
    assert not routing.is_group_enabled(db, -100)
    msg = make_msg()
    await enable(msg)
    assert routing.is_group_enabled(db, -100)
    assert not any(c.args and c.args[0] == -200  # operator not re-asked
                   for c in msg.bot.send_message.call_args_list)


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
    await enable(make_msg(user_id=999))
    assert routing.is_group_enabled(db, -100)
    await disable(make_msg())
    assert not routing.is_group_enabled(db, -100)


def make_req_cb(data, user_id=999):
    return SimpleNamespace(
        data=data,
        bot=SimpleNamespace(send_message=AsyncMock(), set_my_commands=AsyncMock()),
        from_user=SimpleNamespace(id=user_id, username="op"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user_id, type="private"),
            edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


@pytest.fixture
def req_env(tmp_path):
    db = Database(tmp_path / "req.db")
    router = tenancy.build_router(StubHolder(make_config()), db)
    req_cb = router.callback_query.handlers[1].callback  # after lang:
    yield db, req_cb
    db.close()


@pytest.mark.asyncio
async def test_request_approve_enables_and_onboards(req_env):
    db, req_cb = req_env
    db.request_group(-100, owner_user_id=42, title="G")
    cb = make_req_cb("req:approve:-100")
    await req_cb(cb)
    group = db.get_group(-100)
    assert group["enabled"] == 1 and group["approval"] == "approved"
    assert routing.is_group_enabled(db, -100)
    cb.message.edit_text.assert_awaited_once()
    # requester DM'd: approval note + the language chooser
    dms = cb.bot.send_message.call_args_list
    assert dms[0].args[0] == 42 and dms[1].args[0] == 42
    kb = dms[1].kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "lang:en:all"


@pytest.mark.asyncio
async def test_request_reject_notifies_and_blocks_retries(req_env):
    db, req_cb = req_env
    db.request_group(-100, owner_user_id=42, title="G")
    await req_cb(make_req_cb("req:reject:-100"))
    group = db.get_group(-100)
    assert group["enabled"] == 0 and group["approval"] == "rejected"
    assert db.group_approval(-100) == "rejected"


@pytest.mark.asyncio
async def test_request_callback_operator_only_and_idempotent(req_env):
    db, req_cb = req_env
    db.request_group(-100, owner_user_id=42, title="G")

    # a non-operator presser (even the requester) changes nothing
    cb = make_req_cb("req:approve:-100", user_id=42)
    await req_cb(cb)
    assert db.group_approval(-100) == "pending"
    assert cb.answer.call_args.kwargs.get("show_alert") is True

    # decide, then press again → "already decided", state unchanged
    await req_cb(make_req_cb("req:approve:-100"))
    cb2 = make_req_cb("req:reject:-100")
    await req_cb(cb2)
    assert db.group_approval(-100) == "approved"  # reject after approve ignored
    cb2.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_requests_count_toward_owner_cap(tmp_path):
    db = Database(tmp_path / "cap.db")
    cfg = make_config()
    cfg.tenancy.max_groups_per_owner = 2
    router = tenancy.build_router(StubHolder(cfg), db)
    enable = router.message.handlers[0].callback

    db.request_group(-1, owner_user_id=42)
    db.request_group(-2, owner_user_id=42)
    msg = make_msg(chat_id=-3, user_id=42)
    await enable(msg)
    assert db.get_group(-3) is None  # capped by two pending requests
    assert "limit" in msg.reply.call_args[0][0]
    db.close()


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
