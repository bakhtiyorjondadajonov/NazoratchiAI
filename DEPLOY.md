# Deployment

## Go-live checklist (do these IN ORDER — the bot ships in dry-run for a reason)

1. **Create the bot**: @BotFather → `/newbot` → put the token in `.env`.
   In every guarded group the bot must be an **administrator** with
   **Ban users** and **Invite users via link** rights. Add the bot to your
   private operator chat (`bot.admin_chat_id` in `config.yaml`).
2. **Smoke test** in a private test group: run `/enable` as a group admin,
   join with a spare account, confirm the screening report arrives with
   working buttons, and check `logs/decisions.jsonl`.
3. **Calibrate thresholds** (`scripts/calibrate.py scan` / `sweep` / `probe`)
   against a labeled image set — see README "Calibration". Do not skip this:
   the shipped thresholds are educated starting points, not validated values.
4. **Dry-run soak**: run against real traffic for several days with
   `mode.dry_run: true` (the default) and compare the "would be" reports to
   what you'd decide yourself.
5. **Flip live**: set `mode.dry_run: false`, then reload
   (`docker compose kill -s SIGHUP nazoratchi`) or restart.

> **⚠️ Single instance only.** Never run two copies with the same bot token
> (e.g., Docker *and* systemd, or two servers): Telegram long polling would
> split updates between them and SQLite is single-writer. One replica, always.

## Prerequisites

A VPS (1–2 GB RAM) with Docker Engine + the compose plugin. No inbound ports
are needed — the bot uses long polling.

## Quick start (Docker Compose)

```bash
git clone <repo-url> nazoratchi && cd nazoratchi
cp .env.example .env            # fill in GK_BOT_TOKEN, GK_GEMINI_KEY
cp config.example.yaml config.yaml   # set admin_chat_id + admin_user_ids
docker compose up -d --build
docker compose logs -f          # watch self-checks; expect "🟢 NazoratchiAI online"
```

The SQLite database, WAL sidecars and the healthcheck heartbeat live in
`./data`; logs (app log + `decisions.jsonl` audit trail) in `./logs`.

## Operations

- **Reload config** (thresholds/keywords/allowlists, no restart):
  `docker compose kill -s SIGHUP nazoratchi`.
  Caveat: `config.yaml` is bind-mounted as a single file — editors that
  *replace* the file (new inode) leave the container reading the old one.
  Edit in place (`nano`, `sed -i`) or just `docker compose restart`.
- **Update**: `git pull && docker compose up -d --build`.
- **Backup** (WAL-safe, run from cron):
  `sqlite3 data/nazoratchi.db ".backup /backups/gk-$(date +\%F).db"`
  — plus copies of `config.yaml` and `.env`.
- **Health**: `docker ps` shows `(healthy)` when the event loop touched the
  heartbeat file within 90 s; compose restarts the container on failure.
- **Bad token**: the bot fails loudly at startup and compose will restart it
  in a loop — check `docker compose logs` first if the container keeps dying.

## Bare-metal (systemd) alternative

See the install steps in the header of `deploy/nazoratchi.service`.
`systemctl reload nazoratchi` sends SIGHUP for config reload; `Restart=always`
supervises crashes.
