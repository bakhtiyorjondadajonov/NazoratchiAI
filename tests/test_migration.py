"""Schema v1 → v2 migration: the old source CHECK must be removed without
losing data."""

import sqlite3

from gatekeeper.db import Database

V1_SCHEMA = """
CREATE TABLE screenings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('join_request', 'chat_member')),
    user_chat_id INTEGER,
    bio TEXT,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'resolved')),
    created_at REAL NOT NULL
);
CREATE INDEX idx_screenings_status ON screenings (status);
CREATE INDEX idx_screenings_user ON screenings (chat_id, user_id);
CREATE TABLE decisions (
    screening_id INTEGER PRIMARY KEY REFERENCES screenings (id),
    verdict TEXT NOT NULL CHECK (verdict IN ('approve', 'hold', 'decline')),
    signals_json TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    resolved_by TEXT NOT NULL DEFAULT 'auto',
    resolved_at REAL NOT NULL
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '1');
INSERT INTO screenings (chat_id, user_id, source, status, created_at)
    VALUES (-100, 42, 'join_request', 'resolved', 1.0);
INSERT INTO decisions VALUES (1, 'approve', '[]', 'approved', 'auto', 2.0);
"""


def test_v1_database_migrates_and_accepts_new_source(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.close()

    db = Database(path)
    # version bumped
    raw = sqlite3.connect(path)
    version = raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert version == "2"
    # old data intact, FK-clean
    assert raw.execute("SELECT COUNT(*) FROM screenings").fetchone()[0] == 1
    assert raw.execute("SELECT verdict FROM decisions WHERE screening_id=1").fetchone()[0] == "approve"
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    raw.close()

    # the whole point: the new source value now inserts
    sid = db.create_screening(chat_id=-100, user_id=43, source="first_message",
                              user_chat_id=43, bio="hi", first_name="A",
                              last_name=None, username=None)
    assert db.get_screening(sid)["source"] == "first_message"
    db.close()


def test_fresh_database_is_v2_and_validates_source(tmp_path):
    import pytest
    db = Database(tmp_path / "new.db")
    with pytest.raises(ValueError, match="invalid screening source"):
        db.create_screening(chat_id=-1, user_id=1, source="bogus",
                            user_chat_id=None, bio=None, first_name=None,
                            last_name=None, username=None)
    db.close()
