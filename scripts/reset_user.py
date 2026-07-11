"""Forget a user (or a whole chat) so the bot screens them again — testing aid.

Clears the DB-side once-only guards, then REQUIRES a bot restart
(docker compose restart nazoratchi): the first-message handler also keeps an
in-process LRU fast-path that only a restart empties.
- first_message_checks  → the next message counts as a "first message" again
- allowlist             → a previous Keep/Approve no longer exempts them
- screenings + decisions/detections/admin_messages → recently_admitted no
  longer suppresses the join re-screen, and /blocked /held forget the case

Usage:
    python scripts/reset_user.py --db data/nazoratchi.db --chat -100123456 [--user 981331281]

Omit --user to forget EVERY user of that chat. The bot may hold the DB open —
that's fine (WAL); the script only needs the file path. Restart the bot after.
"""

from __future__ import annotations

import argparse
import sqlite3


def reset(db_path: str, chat_id: int, user_id: int | None) -> None:
    conn = sqlite3.connect(db_path)
    user_sql, params = ("", [chat_id]) if user_id is None else (
        " AND user_id = ?", [chat_id, user_id])
    with conn:
        sids = [r[0] for r in conn.execute(
            f"SELECT id FROM screenings WHERE chat_id = ?{user_sql}", params)]
        if sids:
            marks = ",".join("?" * len(sids))
            for table, col in (("detections", "screening_id"),
                               ("admin_messages", "screening_id"),
                               ("decisions", "screening_id"),
                               ("first_message_checks", "screening_id")):
                conn.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", sids)
            conn.execute(f"DELETE FROM screenings WHERE id IN ({marks})", sids)
        for table in ("first_message_checks", "allowlist"):
            conn.execute(f"DELETE FROM {table} WHERE chat_id = ?{user_sql}", params)
    print(f"forgot {len(sids)} screening(s) for chat {chat_id}"
          + (f", user {user_id}" if user_id else " (all users)"))
    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/nazoratchi.db")
    p.add_argument("--chat", type=int, required=True)
    p.add_argument("--user", type=int, default=None)
    args = p.parse_args()
    reset(args.db, args.chat, args.user)
