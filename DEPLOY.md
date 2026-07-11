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

## Railway (no card — ~2-week trial)

Railway's one-time $5 trial needs no card and fits this bot (no forced sleep,
worker without a port, volume for the database). At ~1 GB RAM the credit lasts
roughly **15–19 days** — a proving ground, not a permanent home.

1. Sign up at railway.com **with your GitHub account**, then visit
   **railway.com/verify** — a verified account gets the *Full Trial*. The
   unverified *Limited Trial* restricts outbound network access, which can
   break Telegram polling itself.
2. New Project → **GitHub Repository** → select this repo (the Dockerfile is
   auto-detected).
3. Service → Settings → **Volumes**: add a volume mounted at **`/app/data`**
   (SQLite database + heartbeat live there; trial cap 0.5 GB is plenty).
4. Service → **Variables**: set
   - `GK_BOT_TOKEN` — from @BotFather
   - `GK_GEMINI_KEY` — from Google AI Studio
   - `GK_CONFIG_YAML` — paste the FULL contents of your filled-in
     `config.example.yaml` (admin ids set!), and change one line for Railway:
     **`logging.dir: data/logs`** — the container filesystem is wiped on every
     deploy, so the decisions audit log must live on the volume. (App logs
     also stream to Railway's log view.)
5. Deploy → open the logs → expect `🟢 NazoratchiAI online`.
6. Config changes: edit `GK_CONFIG_YAML` in the dashboard and redeploy
   (the env-delivered config is re-written at every boot; SIGHUP reload
   applies only to hand-edited files on a VM).
7. **Back up before the credit runs out** (~day 15): `railway ssh` →
   `sqlite3 data/nazoratchi.db ".backup data/backup.db"`, download it, and
   note that trial volumes are deleted 30 days after credits expire.

**When the trial ends** — same image, three sustainable homes: Railway Hobby
(~$11–12/mo, post-paid card required), Fly.io (~$6/mo), or an Oracle
Always Free ARM VM ($0). Migrating = moving `.env`, the config, and the
SQLite file.

## Bare-metal (systemd) alternative

See the install steps in the header of `deploy/nazoratchi.service`.
`systemctl reload nazoratchi` sends SIGHUP for config reload; `Restart=always`
supervises crashes.
