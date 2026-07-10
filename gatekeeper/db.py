"""SQLite persistence: pending screenings, decisions/audit log, raw detections,
allowlist and admin report message ids.

Plain sqlite3 in WAL mode guarded by a lock — every operation is a
sub-millisecond point read/write, so blocking the event loop briefly is fine
for a single-process bot and avoids an extra service.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

VALID_SOURCES = ("join_request", "chat_member", "first_message")

# NOTE: screenings.source has no CHECK — the enum lives in code (VALID_SOURCES)
# so adding a source never needs a table rebuild again (v1→v2 migration below).
SCHEMA = """
CREATE TABLE IF NOT EXISTS screenings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    user_chat_id INTEGER,
    bio TEXT,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'resolved')),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screenings_status ON screenings (status);
CREATE INDEX IF NOT EXISTS idx_screenings_user ON screenings (chat_id, user_id);

CREATE TABLE IF NOT EXISTS first_message_checks (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    screening_id INTEGER REFERENCES screenings (id),
    reason TEXT,  -- NULL = screened; 'admin' / 'allowlisted' = exempt, never screened
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS groups (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    owner_user_id INTEGER,  -- the admin who ran /enable = report destination; NULL = operator chat
    is_seed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    screening_id INTEGER PRIMARY KEY REFERENCES screenings (id),
    verdict TEXT NOT NULL CHECK (verdict IN ('approve', 'hold', 'decline')),
    signals_json TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    resolved_by TEXT NOT NULL DEFAULT 'auto',
    resolved_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screening_id INTEGER NOT NULL REFERENCES screenings (id),
    photo_index INTEGER NOT NULL,
    file_unique_id TEXT,
    model TEXT NOT NULL CHECK (model IN ('detector', 'classifier')),
    class TEXT NOT NULL,
    score REAL NOT NULL,
    box_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_detections_screening ON detections (screening_id);

CREATE TABLE IF NOT EXISTS allowlist (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_by TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS admin_messages (
    screening_id INTEGER PRIMARY KEY REFERENCES screenings (id),
    admin_chat_id INTEGER NOT NULL,
    media_message_ids_json TEXT NOT NULL DEFAULT '[]',
    keyboard_message_id INTEGER,
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved'))
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_VERSION = "2"


class Database:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            with self._conn:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.executescript(SCHEMA)
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            self._migrate()

    def _migrate(self) -> None:
        """v1 → v2: rebuild screenings without the source CHECK (it blocked the
        'first_message' source). Standard SQLite 12-step table rebuild."""
        version = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        if version != "1":
            return
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self._conn:
                self._conn.execute("""
                    CREATE TABLE screenings_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        source TEXT NOT NULL,
                        user_chat_id INTEGER,
                        bio TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        username TEXT,
                        status TEXT NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'processing', 'resolved')),
                        created_at REAL NOT NULL
                    )""")
                self._conn.execute("INSERT INTO screenings_new SELECT * FROM screenings")
                self._conn.execute("DROP TABLE screenings")
                self._conn.execute("ALTER TABLE screenings_new RENAME TO screenings")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_screenings_status ON screenings (status)")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_screenings_user ON screenings (chat_id, user_id)")
                self._conn.execute(
                    "UPDATE meta SET value = '2' WHERE key = 'schema_version'")
            violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"migration left FK violations: {violations[:5]}")
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    # -- screenings ----------------------------------------------------------

    def create_screening(
        self,
        *,
        chat_id: int,
        user_id: int,
        source: str,
        user_chat_id: int | None,
        bio: str | None,
        first_name: str | None,
        last_name: str | None,
        username: str | None,
    ) -> int:
        if source not in VALID_SOURCES:  # enum enforced in code since schema v2
            raise ValueError(f"invalid screening source: {source}")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO screenings (chat_id, user_id, source, user_chat_id, bio,"
                " first_name, last_name, username, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, user_id, source, user_chat_id, bio, first_name, last_name,
                 username, time.time()),
            )
            return cur.lastrowid

    def get_screening(self, screening_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM screenings WHERE id = ?", (screening_id,)
            ).fetchone()

    def set_status(self, screening_id: int, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE screenings SET status = ? WHERE id = ?", (status, screening_id)
            )

    def unresolved_screenings(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM screenings WHERE status != 'resolved' ORDER BY id"
            ).fetchall()

    def unresolved_screening_exists(self, chat_id: int, user_id: int) -> bool:
        """Dedupe guard: a screening for this user is currently queued/processing."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM screenings WHERE chat_id = ? AND user_id = ?"
                " AND status != 'resolved' LIMIT 1",
                (chat_id, user_id),
            ).fetchone()
            return row is not None

    def recently_admitted(self, chat_id: int, user_id: int, within_s: float) -> bool:
        """True if we approved/kept this user recently — their chat_member join
        echo must not trigger a second screening (via_join_request is unset for
        invite-link join requests, so the decision record is the real guard)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM screenings s JOIN decisions d ON d.screening_id = s.id"
                " WHERE s.chat_id = ? AND s.user_id = ?"
                " AND d.action_taken IN ('approved', 'resolved_externally', 'kept', 'dry_run')"
                " AND d.resolved_at > ? LIMIT 1",
                (chat_id, user_id, time.time() - within_s),
            ).fetchone()
            return row is not None

    # -- decisions -----------------------------------------------------------

    def record_decision(
        self,
        screening_id: int,
        *,
        verdict: str,
        signals: list[dict[str, Any]],
        action_taken: str,
        resolved_by: str = "auto",
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO decisions (screening_id, verdict, signals_json, action_taken,"
                " resolved_by, resolved_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (screening_id) DO UPDATE SET"
                " verdict = excluded.verdict, signals_json = excluded.signals_json,"
                " action_taken = excluded.action_taken, resolved_by = excluded.resolved_by,"
                " resolved_at = excluded.resolved_at",
                (screening_id, verdict, json.dumps(signals, ensure_ascii=False),
                 action_taken, resolved_by, time.time()),
            )

    def update_action(self, screening_id: int, action_taken: str, resolved_by: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE decisions SET action_taken = ?, resolved_by = ?, resolved_at = ?"
                " WHERE screening_id = ?",
                (action_taken, resolved_by, time.time(), screening_id),
            )

    def get_decision(self, screening_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM decisions WHERE screening_id = ?", (screening_id,)
            ).fetchone()

    def list_by_action(self, action_takens: tuple[str, ...], limit: int = 20,
                       chat_ids: list[int] | None = None) -> list[sqlite3.Row]:
        """Current blocked/held cases for the /blocked and /held admin commands.
        action_taken reflects the LATEST state (admin actions overwrite it),
        so 'banned' rows are users who are still banned right now.
        chat_ids=None → all groups (operator scope)."""
        placeholders = ",".join("?" * len(action_takens))
        params: list = [*action_takens]
        chat_filter = ""
        if chat_ids is not None:
            if not chat_ids:
                return []
            chat_filter = f" AND s.chat_id IN ({','.join('?' * len(chat_ids))})"
            params.extend(chat_ids)
        params.append(limit)
        with self._lock:
            return self._conn.execute(
                f"SELECT s.*, d.verdict, d.action_taken, d.resolved_at, d.signals_json"
                f" FROM decisions d JOIN screenings s ON s.id = d.screening_id"
                f" WHERE d.action_taken IN ({placeholders}){chat_filter}"
                f" ORDER BY d.resolved_at DESC LIMIT ?",
                params,
            ).fetchall()

    # -- first-message screening ----------------------------------------------

    def first_message_done(self, chat_id: int, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM first_message_checks WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            return row is not None

    def claim_first_message(self, chat_id: int, user_id: int) -> bool:
        """Atomic once-only claim; True if this caller won the claim."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO first_message_checks (chat_id, user_id, created_at)"
                " VALUES (?, ?, ?)",
                (chat_id, user_id, time.time()),
            )
            return cur.rowcount > 0

    def mark_first_message(self, chat_id: int, user_id: int, reason: str) -> None:
        """Record an exemption (admin/allowlisted) so the user is never scanned."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO first_message_checks"
                " (chat_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, user_id, reason, time.time()),
            )

    def set_first_message_screening(self, chat_id: int, user_id: int,
                                    screening_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE first_message_checks SET screening_id = ?"
                " WHERE chat_id = ? AND user_id = ?",
                (screening_id, chat_id, user_id),
            )

    # -- groups (multi-tenancy) ------------------------------------------------

    def ensure_group(self, chat_id: int, title: str | None) -> None:
        """Row exists (disabled by default); refresh the title."""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO groups (chat_id, title, enabled, is_seed, created_at, updated_at)"
                " VALUES (?, ?, 0, 0, ?, ?)"
                " ON CONFLICT (chat_id) DO UPDATE SET"
                " title = COALESCE(excluded.title, groups.title), updated_at = excluded.updated_at",
                (chat_id, title, now, now),
            )

    def enable_group(self, chat_id: int, owner_user_id: int | None,
                     title: str | None = None, is_seed: bool = False) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO groups (chat_id, title, enabled, owner_user_id, is_seed,"
                " created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?, ?)"
                " ON CONFLICT (chat_id) DO UPDATE SET"
                " enabled = 1, owner_user_id = excluded.owner_user_id,"
                " is_seed = excluded.is_seed,"
                " title = COALESCE(excluded.title, groups.title),"
                " updated_at = excluded.updated_at",
                (chat_id, title, owner_user_id, int(is_seed), now, now),
            )

    def set_group_enabled(self, chat_id: int, enabled: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE groups SET enabled = ?, updated_at = ? WHERE chat_id = ?",
                (int(enabled), time.time(), chat_id),
            )

    def get_group(self, chat_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM groups WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def enabled_groups(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM groups WHERE enabled = 1 ORDER BY chat_id"
            ).fetchall()

    def count_enabled_by_owner(self, user_id: int) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM groups WHERE owner_user_id = ? AND enabled = 1",
                (user_id,),
            ).fetchone()[0]

    def groups_owned_by(self, user_id: int) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id FROM groups WHERE owner_user_id = ? AND enabled = 1",
                (user_id,),
            ).fetchall()
            return [r["chat_id"] for r in rows]

    # -- detections ----------------------------------------------------------

    def add_detections(self, screening_id: int, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO detections (screening_id, photo_index, file_unique_id,"
                " model, class, score, box_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (screening_id, r["photo_index"], r.get("file_unique_id"), r["model"],
                     r["class"], r["score"],
                     json.dumps(r.get("box")) if r.get("box") is not None else None)
                    for r in rows
                ],
            )

    # -- allowlist -----------------------------------------------------------

    def is_allowlisted(self, chat_id: int, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM allowlist WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            return row is not None

    def allowlist_add(self, chat_id: int, user_id: int, added_by: str, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO allowlist (chat_id, user_id, added_by, reason,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_id, added_by, reason, time.time()),
            )

    # -- admin messages ------------------------------------------------------

    def save_admin_messages(
        self,
        screening_id: int,
        admin_chat_id: int,
        media_message_ids: list[int],
        keyboard_message_id: int | None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO admin_messages (screening_id, admin_chat_id,"
                " media_message_ids_json, keyboard_message_id, state)"
                " VALUES (?, ?, ?, ?, 'open')",
                (screening_id, admin_chat_id, json.dumps(media_message_ids),
                 keyboard_message_id),
            )

    def get_admin_messages(self, screening_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM admin_messages WHERE screening_id = ?", (screening_id,)
            ).fetchone()

    def resolve_admin_messages(self, screening_id: int) -> bool:
        """Mark resolved; returns False if it already was (idempotency guard)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE admin_messages SET state = 'resolved'"
                " WHERE screening_id = ? AND state = 'open'",
                (screening_id,),
            )
            return cur.rowcount > 0

    def reopen_admin_messages(self, screening_id: int) -> None:
        """Give the buttons back after a failed action so the admin can retry."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE admin_messages SET state = 'open' WHERE screening_id = ?",
                (screening_id,),
            )
