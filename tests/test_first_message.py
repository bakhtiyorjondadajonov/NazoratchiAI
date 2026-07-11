"""First-message screening decision table (plan §B)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nazoratchi.config import ConfigHolder
from nazoratchi.db import Database
from nazoratchi.handlers import first_message

from .conftest import make_config


class StubOrchestrator:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, screening_id):
        self.enqueued.append(screening_id)


class StubHolder:
    def __init__(self, cfg):
        self.current = cfg


def make_msg(chat_id=-100, user_id=42, is_bot=False, member_status="member",
             bio="some bio", text="hello there", photo=None, caption=None,
             message_id=1):
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status=member_status)),
        get_chat=AsyncMock(return_value=SimpleNamespace(bio=bio)),
    )
    user = SimpleNamespace(id=user_id, is_bot=is_bot, first_name="Test",
                           last_name=None, username="test_user")
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id, type="supergroup"),
                           from_user=user, bot=bot, message_id=message_id,
                           text=text, photo=photo, caption=caption)


def photo_sizes():
    return [SimpleNamespace(file_id="small", file_unique_id="u-small",
                            width=90, height=90),
            SimpleNamespace(file_id="big", file_unique_id="u-big",
                            width=800, height=800)]


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.enable_group(-100, owner_user_id=777, title="Test Group")
    orch = StubOrchestrator()
    router = first_message.build_router(StubHolder(make_config()), db, orch)
    handler = router.message.handlers[0].callback
    yield db, orch, handler
    db.close()


async def run(handler, msg):
    await handler(msg)


@pytest.mark.asyncio
async def test_normal_member_screened_once(env):
    db, orch, handler = env
    msg = make_msg()
    await run(handler, msg)
    assert len(orch.enqueued) == 1
    row = db.get_screening(orch.enqueued[0])
    assert row["source"] == "first_message"
    assert row["bio"] == "some bio"
    # second message: no new screening
    await run(handler, make_msg())
    assert len(orch.enqueued) == 1


@pytest.mark.asyncio
async def test_survives_restart_without_rescan(env):
    db, orch, handler = env
    await run(handler, make_msg())
    # simulate restart: fresh router (empty LRU), same DB
    router2 = first_message.build_router(
        StubHolder(make_config()), db, orch)
    handler2 = router2.message.handlers[0].callback
    await run(handler2, make_msg())
    assert len(orch.enqueued) == 1  # persisted claim prevented a re-scan


@pytest.mark.asyncio
async def test_bots_and_anonymous_skipped(env):
    db, orch, handler = env
    await run(handler, make_msg(is_bot=True))
    msg = make_msg()
    msg.from_user = None
    await run(handler, msg)
    assert orch.enqueued == []


@pytest.mark.asyncio
async def test_disabled_group_ignored(env):
    db, orch, handler = env
    db.set_group_enabled(-100, False)
    await run(handler, make_msg())
    assert orch.enqueued == []
    assert not db.first_message_done(-100, 42)  # nothing persisted


@pytest.mark.asyncio
async def test_admin_marked_exempt_not_screened(env):
    db, orch, handler = env
    await run(handler, make_msg(member_status="administrator"))
    assert orch.enqueued == []
    assert db.first_message_done(-100, 42)  # marked, never re-checked


@pytest.mark.asyncio
async def test_allowlisted_marked_exempt(env):
    db, orch, handler = env
    db.allowlist_add(-100, 42, "@admin", "test")
    await run(handler, make_msg())
    assert orch.enqueued == []
    assert db.first_message_done(-100, 42)


@pytest.mark.asyncio
async def test_defers_while_join_screening_in_flight(env):
    db, orch, handler = env
    db.create_screening(chat_id=-100, user_id=42, source="chat_member",
                        user_chat_id=None, bio=None, first_name=None,
                        last_name=None, username=None)  # queued = unresolved
    await run(handler, make_msg())
    assert orch.enqueued == []
    assert not db.first_message_done(-100, 42)  # not marked → retried later


@pytest.mark.asyncio
async def test_bio_fetch_failure_still_screens(env):
    db, orch, handler = env
    msg = make_msg()
    msg.bot.get_chat = AsyncMock(side_effect=Exception("chat not found"))
    await run(handler, msg)
    assert len(orch.enqueued) == 1
    assert db.get_screening(orch.enqueued[0])["bio"] is None


# --- content payload extraction (plan §B: text/emoji/photo yes, rest no) ----

import json


@pytest.mark.asyncio
async def test_text_message_payload_persisted(env):
    db, orch, handler = env
    await run(handler, make_msg(text="check my channel", message_id=77))
    ctx = json.loads(db.get_screening(orch.enqueued[0])["context_json"])
    assert ctx == {"message_id": 77, "text": "check my channel",
                   "photo_file_id": None, "photo_unique_id": None}


@pytest.mark.asyncio
async def test_emoji_only_message_payload_persisted(env):
    db, orch, handler = env
    await run(handler, make_msg(text="🍑🔥"))
    ctx = json.loads(db.get_screening(orch.enqueued[0])["context_json"])
    assert ctx["text"] == "🍑🔥"


@pytest.mark.asyncio
async def test_photo_with_caption_payload_persisted(env):
    db, orch, handler = env
    await run(handler, make_msg(text=None, photo=photo_sizes(), caption="hi"))
    ctx = json.loads(db.get_screening(orch.enqueued[0])["context_json"])
    assert ctx["photo_file_id"] == "big"          # largest PhotoSize chosen
    assert ctx["photo_unique_id"] == "u-big"
    assert ctx["text"] == "hi"                     # caption rides as text


@pytest.mark.asyncio
async def test_audio_message_no_payload_profile_only(env):
    db, orch, handler = env
    await run(handler, make_msg(text=None))  # no text, no photo = other type
    assert len(orch.enqueued) == 1                 # profile rescan still runs
    assert db.get_screening(orch.enqueued[0])["context_json"] is None
    # one shot: a later text message does NOT get content-checked
    await run(handler, make_msg(text="porno"))
    assert len(orch.enqueued) == 1
