from pathlib import Path

import pytest

from nazoratchi.config import load_config, materialize_config_from_env
from nazoratchi.db import Database

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repo_config_is_valid():
    # validate the TRACKED template (config.yaml is operator-owned/untracked)
    cfg = load_config(REPO_ROOT / "config.example.yaml")
    assert cfg.mode.dry_run is True  # ships in dry-run until calibrated
    assert cfg.mode.check_first_message_content is True
    assert cfg.photos.max_photos == 5
    assert cfg.tenancy.max_groups_per_owner == 20
    assert cfg.nudenet.decline and cfg.nudenet.hold
    assert "FEMALE_BREAST_COVERED" in cfg.nudenet.hold
    assert cfg.text.hard_keywords.all() and cfg.text.soft_keywords.all()
    # every threshold sane
    for t in {**cfg.nudenet.decline, **cfg.nudenet.hold}.values():
        assert 0.2 <= t <= 0.9


def test_config_materializes_from_env(tmp_path, monkeypatch):
    """Railway-style delivery: full YAML in $GK_CONFIG_YAML is written to disk
    before loading."""
    target = tmp_path / "cfg" / "config.yaml"
    yaml_text = (REPO_ROOT / "config.example.yaml").read_text()

    monkeypatch.delenv("GK_CONFIG_YAML", raising=False)
    assert materialize_config_from_env(target) is False  # no-op when unset
    assert not target.exists()

    monkeypatch.setenv("GK_CONFIG_YAML", yaml_text)
    assert materialize_config_from_env(target) is True
    cfg = load_config(target)  # parses to a valid AppConfig
    assert cfg.photos.max_photos == 5


def test_config_rejects_class_in_both_tiers(tmp_path):
    import yaml
    raw = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text())
    raw["nudenet"]["hold"]["FEMALE_GENITALIA_EXPOSED"] = 0.5  # also in decline
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(Exception, match="both"):
        load_config(bad)


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def test_screening_roundtrip(db):
    sid = db.create_screening(
        chat_id=-100, user_id=42, source="join_request", user_chat_id=42,
        bio="hello", first_name="A", last_name=None, username="a42",
    )
    row = db.get_screening(sid)
    assert row["status"] == "queued" and row["bio"] == "hello"

    db.set_status(sid, "processing")
    assert db.get_screening(sid)["status"] == "processing"
    assert [r["id"] for r in db.unresolved_screenings()] == [sid]

    db.record_decision(sid, verdict="hold", signals=[{"kind": "covered_hit"}],
                       action_taken="pending")
    db.set_status(sid, "resolved")
    assert db.unresolved_screenings() == []
    assert db.get_decision(sid)["verdict"] == "hold"

    db.update_action(sid, "approved", "@admin")
    d = db.get_decision(sid)
    assert d["action_taken"] == "approved" and d["resolved_by"] == "@admin"


def test_dedupe_guard_unresolved_only(db):
    sid = db.create_screening(chat_id=-100, user_id=42, source="join_request",
                              user_chat_id=None, bio=None, first_name=None,
                              last_name=None, username=None)
    assert db.unresolved_screening_exists(-100, 42)
    assert not db.unresolved_screening_exists(-100, 43)
    # once resolved, a fresh request must be allowed to screen again
    db.set_status(sid, "resolved")
    assert not db.unresolved_screening_exists(-100, 42)


def test_recently_admitted_guards_join_echo(db):
    sid = db.create_screening(chat_id=-100, user_id=42, source="join_request",
                              user_chat_id=None, bio=None, first_name=None,
                              last_name=None, username=None)
    assert not db.recently_admitted(-100, 42, within_s=3600)
    db.record_decision(sid, verdict="approve", signals=[], action_taken="approved")
    db.set_status(sid, "resolved")
    assert db.recently_admitted(-100, 42, within_s=3600)
    assert not db.recently_admitted(-101, 42, within_s=3600)


def test_declined_user_is_not_recently_admitted(db):
    sid = db.create_screening(chat_id=-100, user_id=42, source="join_request",
                              user_chat_id=None, bio=None, first_name=None,
                              last_name=None, username=None)
    db.record_decision(sid, verdict="decline", signals=[], action_taken="declined")
    assert not db.recently_admitted(-100, 42, within_s=3600)


def test_list_by_action_for_admin_commands(db):
    banned = db.create_screening(chat_id=-100, user_id=1, source="chat_member",
                                 user_chat_id=None, bio=None, first_name="Bad",
                                 last_name=None, username=None)
    db.record_decision(banned, verdict="decline", signals=[{"kind": "exposed_hit"}],
                       action_taken="banned")
    held = db.create_screening(chat_id=-100, user_id=2, source="chat_member",
                               user_chat_id=None, bio=None, first_name="Maybe",
                               last_name=None, username=None)
    db.record_decision(held, verdict="hold", signals=[{"kind": "covered_hit"}],
                       action_taken="banned_pending")

    blocked_rows = db.list_by_action(("banned", "declined"))
    assert [r["user_id"] for r in blocked_rows] == [1]
    held_rows = db.list_by_action(("pending", "banned_pending"))
    assert [r["user_id"] for r in held_rows] == [2]

    # admin unbans → user leaves the blocked list
    db.update_action(banned, "overridden", "@admin")
    assert db.list_by_action(("banned", "declined")) == []


def test_reopen_admin_messages_allows_retry(db):
    sid = db.create_screening(chat_id=-100, user_id=42, source="join_request",
                              user_chat_id=None, bio=None, first_name=None,
                              last_name=None, username=None)
    db.save_admin_messages(sid, -200, [], 3)
    assert db.resolve_admin_messages(sid)
    db.reopen_admin_messages(sid)
    assert db.resolve_admin_messages(sid)  # retry press works again


def test_allowlist(db):
    assert not db.is_allowlisted(-100, 42)
    db.allowlist_add(-100, 42, "@admin", "override")
    assert db.is_allowlisted(-100, 42)
    assert not db.is_allowlisted(-101, 42)  # per-chat


def test_admin_messages_single_writer_lock(db):
    sid = db.create_screening(chat_id=-100, user_id=42, source="join_request",
                              user_chat_id=None, bio=None, first_name=None,
                              last_name=None, username=None)
    db.save_admin_messages(sid, -200, [1, 2], 3)
    assert db.resolve_admin_messages(sid) is True   # first press wins
    assert db.resolve_admin_messages(sid) is False  # second press is a no-op
