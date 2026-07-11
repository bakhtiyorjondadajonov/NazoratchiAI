"""First-message CONTENT screening through the orchestrator (plan §5)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nazoratchi.config import LangKeywords, TextCfg
from nazoratchi.db import Database
from nazoratchi.screening.gemini_check import GeminiOutcome
from nazoratchi.screening.nudenet_runtime import InferenceResult
from nazoratchi.screening.orchestrator import Orchestrator

from .conftest import make_config


class Holder:
    def __init__(self, cfg):
        self.current = cfg


def make_cfg(**over):
    cfg = make_config(text=TextCfg(hard_keywords=LangKeywords(en=["porno"])))
    for key, value in over.items():
        setattr(cfg.mode, key, value)
    return cfg


def make_bot():
    async def dl(path, destination):
        destination.write(b"fake-jpeg-bytes")

    return SimpleNamespace(
        get_user_profile_photos=AsyncMock(
            return_value=SimpleNamespace(photos=[], total_count=0)),
        get_file=AsyncMock(return_value=SimpleNamespace(file_path="p")),
        download_file=AsyncMock(side_effect=dl),
        ban_chat_member=AsyncMock(),
    )


def make_runtime(detections=None, unsafe=None, error=None):
    return SimpleNamespace(analyze=AsyncMock(return_value=InferenceResult(
        detections=detections or [], classifier_unsafe=unsafe, error=error)))


def make_gemini():
    return SimpleNamespace(classify=AsyncMock(
        return_value=GeminiOutcome(status="disabled")))


@pytest.fixture
def report_spy(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr("nazoratchi.notifier.report", spy)
    return spy


def make_env(tmp_path, cfg, runtime=None):
    db = Database(tmp_path / "c.db")
    bot = make_bot()
    orch = Orchestrator(bot, db, Holder(cfg), runtime or make_runtime(), make_gemini())
    return db, bot, orch


def add_screening(db, context):
    return db.create_screening(
        chat_id=-100, user_id=42, source="first_message", user_chat_id=42,
        bio=None, first_name="Test", last_name=None, username="test_user",
        context=context)


@pytest.mark.asyncio
async def test_content_text_hard_held_never_bans(tmp_path, report_spy):
    """Owner's rule: a hard keyword reports to the admin (Ban/Keep buttons)
    but never auto-bans — words alone must not remove a real person."""
    db, bot, orch = make_env(tmp_path, make_cfg())
    sid = add_screening(db, {"message_id": 1, "text": "kanal: porno 24/7",
                             "photo_file_id": None, "photo_unique_id": None})
    await orch._process(sid)
    d = db.get_decision(sid)
    assert d["verdict"] == "hold" and d["action_taken"] == "kept_flagged"
    bot.ban_chat_member.assert_not_awaited()
    assert 'message: keyword' in d["signals_json"]


@pytest.mark.asyncio
async def test_content_photo_exposed_banned_with_evidence(tmp_path, report_spy):
    runtime = make_runtime(detections=[
        {"class": "FEMALE_BREAST_EXPOSED", "score": 0.9, "box": None}])
    db, bot, orch = make_env(tmp_path, make_cfg(), runtime)
    sid = add_screening(db, {"message_id": 2, "text": None,
                             "photo_file_id": "msgphoto", "photo_unique_id": "u1"})
    await orch._process(sid)
    d = db.get_decision(sid)
    assert d["verdict"] == "decline" and d["action_taken"] == "banned"
    assert "message photo: FEMALE_BREAST_EXPOSED" in d["signals_json"]
    # posted photo included as report evidence; detections row marked -1
    assert report_spy.await_args.kwargs["flagged_file_ids"] == ["msgphoto"]


@pytest.mark.asyncio
async def test_content_photo_covered_bans_pending_and_deletes(tmp_path, report_spy):
    """Owner's policy: photos act, words ask — a photo-backed hold bans
    immediately (reversible via Unban) and deletes the first message."""
    runtime = make_runtime(detections=[
        {"class": "FEMALE_BREAST_COVERED", "score": 0.6, "box": None}])
    db, bot, orch = make_env(tmp_path, make_cfg(), runtime)
    sid = add_screening(db, {"message_id": 3, "text": None,
                             "photo_file_id": "f", "photo_unique_id": "u"})
    await orch._process(sid)
    d = db.get_decision(sid)
    assert d["verdict"] == "hold" and d["action_taken"] == "banned_pending"
    bot.ban_chat_member.assert_awaited_once_with(
        chat_id=-100, user_id=42, revoke_messages=True)


@pytest.mark.asyncio
async def test_gemini_adult_hold_keeps_user_in(tmp_path, report_spy):
    """A text judged an ad by Gemini holds WITHOUT banning (words ask)."""
    cfg = make_config(text=TextCfg(soft_keywords=LangKeywords(en=["nudes"])))
    db, bot, _ = make_env(tmp_path, cfg)
    from nazoratchi.screening.gemini_check import BioVerdict
    gemini = SimpleNamespace(classify=AsyncMock(return_value=GeminiOutcome(
        status="ok", verdict=BioVerdict(is_adult=True, confidence=0.95,
                                        reason="adult-service ad"))))
    orch = Orchestrator(bot, db, Holder(cfg), make_runtime(), gemini)
    sid = add_screening(db, {"message_id": 9, "text": "selling nudes cheap",
                             "photo_file_id": None, "photo_unique_id": None})
    await orch._process(sid)
    d = db.get_decision(sid)
    assert d["verdict"] == "hold" and d["action_taken"] == "kept_flagged"
    bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_clean_kept(tmp_path, report_spy):
    db, bot, orch = make_env(tmp_path, make_cfg())
    sid = add_screening(db, {"message_id": 4, "text": "salom hammaga!",
                             "photo_file_id": None, "photo_unique_id": None})
    await orch._process(sid)
    d = db.get_decision(sid)
    assert d["verdict"] == "approve" and d["action_taken"] == "kept"
    bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_photo_download_failure_kept_flagged(tmp_path, report_spy):
    db, bot, orch = make_env(tmp_path, make_cfg())
    bot.get_file = AsyncMock(side_effect=Exception("message deleted"))
    sid = add_screening(db, {"message_id": 5, "text": None,
                             "photo_file_id": "gone", "photo_unique_id": "u"})
    await orch._process(sid)
    d = db.get_decision(sid)
    # infra failure never bans — flagged for the admin instead
    assert d["verdict"] == "hold" and d["action_taken"] == "kept_flagged"
    bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_off_payload_ignored(tmp_path, report_spy):
    db, bot, orch = make_env(
        tmp_path, make_cfg(check_first_message_content=False))
    sid = add_screening(db, {"message_id": 6, "text": "porno porno",
                             "photo_file_id": None, "photo_unique_id": None})
    await orch._process(sid)
    d = db.get_decision(sid)
    assert d["verdict"] == "approve" and d["action_taken"] == "kept"


@pytest.mark.asyncio
async def test_resume_processes_persisted_context(tmp_path, report_spy):
    """Restart between message and processing loses nothing."""
    db, bot, orch = make_env(tmp_path, make_cfg())
    sid = add_screening(db, {"message_id": 7, "text": "the best porno here",
                             "photo_file_id": None, "photo_unique_id": None})
    # simulate restart: a brand-new orchestrator resumes from the DB
    bot2 = make_bot()
    orch2 = Orchestrator(bot2, db, Holder(make_cfg()), make_runtime(), make_gemini())
    assert orch2.resume_pending() == 1
    await orch2._process(sid)
    # keyword hit → held for the admin (never auto-banned)
    assert db.get_decision(sid)["action_taken"] == "kept_flagged"
