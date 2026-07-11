"""Schema v1 → v2 migration: the old source CHECK must be removed without
losing data."""

import sqlite3

from nazoratchi.db import Database

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


def _columns(raw, table):
    return [r[1] for r in raw.execute(f"PRAGMA table_info({table})")]


def test_v1_database_migrates_through_full_chain(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.close()

    db = Database(path)
    raw = sqlite3.connect(path)
    version = raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert version == "3"  # v1 → v2 → v3 chain
    assert "context_json" in _columns(raw, "screenings")
    # old data intact, FK-clean
    assert raw.execute("SELECT COUNT(*) FROM screenings").fetchone()[0] == 1
    assert raw.execute("SELECT verdict FROM decisions WHERE screening_id=1").fetchone()[0] == "approve"
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    raw.close()

    # new source + context both insert
    sid = db.create_screening(chat_id=-100, user_id=43, source="first_message",
                              user_chat_id=43, bio="hi", first_name="A",
                              last_name=None, username=None,
                              context={"message_id": 5, "text": "yo"})
    row = db.get_screening(sid)
    assert row["source"] == "first_message"
    assert '"yo"' in row["context_json"]
    db.close()


def test_v2_database_migrates_to_v3(tmp_path):
    """A DB created by the previous release (v2: no source CHECK, no
    context_json) gets the column via plain ALTER."""
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA.replace(
        "source TEXT NOT NULL CHECK (source IN ('join_request', 'chat_member'))",
        "source TEXT NOT NULL",
    ).replace("INSERT INTO meta VALUES ('schema_version', '1');",
              "INSERT INTO meta VALUES ('schema_version', '2');"))
    conn.close()

    db = Database(path)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    assert "context_json" in _columns(raw, "screenings")
    assert raw.execute("SELECT COUNT(*) FROM screenings").fetchone()[0] == 1
    raw.close()
    db.close()


def test_fresh_database_is_v3_and_validates_source(tmp_path):
    import pytest
    path = tmp_path / "new.db"
    db = Database(path)
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    assert "context_json" in _columns(raw, "screenings")
    raw.close()
    with pytest.raises(ValueError, match="invalid screening source"):
        db.create_screening(chat_id=-1, user_id=1, source="bogus",
                            user_chat_id=None, bio=None, first_name=None,
                            last_name=None, username=None)
    db.close()
