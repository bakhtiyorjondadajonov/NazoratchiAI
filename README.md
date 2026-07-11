# NazoratchiAI

![CI](https://github.com/bakhtiyorjondadajonov/NazoratchiAI/actions/workflows/ci.yml/badge.svg)

Screens every user who enters a Telegram group and removes porn-advertising
accounts. Exactly one job — after a user passes screening, the bot does nothing.

Two operating modes, both always active:

- **Open-join (primary)**: users join instantly with no approval wait. The bot
  screens them right after entry; a bad profile → **immediate permanent ban**
  + admin report with an Unban button for mistakes.
- **Join-request (optional)**: if the group has "Approve new members" enabled,
  the bot screens *before* entry and approves/declines/holds the request.

Plus a **first-message re-screen**: each member is screened one more time when
they send their first message — by then their bio is readable (it isn't at
join time) and a photo swapped after joining gets caught. A ban on this path
also deletes the offending message.

**Self-serve multi-tenant**: any group admin can add the bot and run
`/enable` in their group (after pressing Start in the bot's DM — that DM
becomes where the group's reports go). `/disable` turns it off. Groups listed
in `chats.allowed` are *seed* groups: auto-enabled, reported to the operator
chat. Per-admin cap: `tenancy.max_groups_per_owner` (default 20).

**Checks per joining user:**

1. **Profile photos** — the 5 most recent profile photos (configurable via
   `photos.max_photos`) are scanned locally by a NudeNet
   ensemble (v3 320n detector + v2 safe/unsafe classifier). Exposed nudity →
   auto-decline. Underwear / lingerie / bikini classes → held for admin review.
   No images ever leave the machine.
2. **Bio + name + username text** — normalized against obfuscation
   (Cyrillic/Latin homoglyphs, zero-width chars, leetspeak, spacing tricks) and
   matched against keyword tiers in Uzbek (both scripts), Russian and English.
   Unambiguous hits auto-decline; ambiguous ones are classified by Gemini
   (`gemini-2.5-flash-lite`, structured JSON, ~$0.05 per 1,000 checks).
3. **Stories** — *not possible*: the Bot API provides no way to read any
   user's stories (see Accepted limitations).

Every decision is logged to disk (`logs/decisions.jsonl`) and reported to the
admin chat with the evidence, the exact classes/scores that fired, and inline
buttons (Approve / Decline / Override / Unban / Kick).

## Deploying to production

See **[DEPLOY.md](DEPLOY.md)** — Docker Compose quick-start, the go-live
checklist (smoke test → calibration → dry-run soak → flip live), config
reload, backups, and a systemd alternative.

## Setup (development)

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# v2 classifier second opinion (83 MB; strongly recommended — catches
# anime/drawn content and detector misses):
curl -L -o models/classifier_model.onnx \
  https://github.com/notAI-tech/NudeNet/releases/download/v0/classifier_model.onnx

export GK_BOT_TOKEN="123456:ABC..."   # from @BotFather
export GK_GEMINI_KEY="..."            # from Google AI Studio (optional but recommended)
```

Edit `config.yaml`:

- `bot.admin_chat_id` — private group/channel for reports (add the bot there)
- `bot.admin_user_ids` — who may press the action buttons
- `chats.allowed` — optional seed groups (other groups self-serve via `/enable`)

Telegram-side setup for each guarded group:

1. Add the bot as **administrator** with the **"Ban users"** right (without it
   screening is toothless — checked at startup) and the **"Invite users via
   link"** right (needed for override invite links and join-request mode).
2. For **open-join mode** (users don't wait): leave "Approve new members" OFF —
   nothing else needed. For **join-request mode** (screen before entry): turn
   it ON.

Run:

```bash
.venv/bin/nazoratchi --config config.yaml
# or: .venv/bin/python -m nazoratchi --config config.yaml
```

`kill -HUP <pid>` re-loads thresholds / keywords / allowlists without a restart.

## Decision policy

| Signal | Open-join (user is inside) | Join-request (user is outside) |
|---|---|---|
| `*_EXPOSED` nudity class ≥ threshold (0.25–0.28), or hard text hit (keyword, 🔞, onlyfans/fansly link) | **permanent ban** + report with Unban button | auto-decline + report with Override button |
| Underwear/covered class ≥ threshold (0.30–0.45), `MALE_BREAST_EXPOSED` ≥ 0.45, belly-combo, classifier-unsafe ≥ 0.70, Gemini says adult | **permanent ban, pending review** + report with Unban button | request stays pending; admin gets Approve/Decline buttons |
| Photo download failure, screening crash | kept in, flagged to admin | request held for admin |
| Nothing fired (incl. no visible photo / empty bio) | kept | auto-approve |

Admin **Unban / Approve / Override** also add the user to a per-chat allowlist
so they are never screened into the same trap again.

Admin commands (in the admin chat): **`/blocked`** lists currently banned/declined
users with one-tap Unban buttons; **`/held`** lists cases awaiting review.

Note: in open-join mode the user's **bio is not readable** (Telegram only
exposes it on join requests) — the text check covers name + username there.

## Calibration (required before going live)

The bot ships with `mode.dry_run: true` — it reports what it *would* do but
takes no action. Keep it that way until:

1. Build a labeled set: `calib_set/positive_nude/`, `calib_set/positive_underwear/`,
   `calib_set/negative/` (tricky normals: portraits, evening dresses, gym/
   leggings, clothed beach, group shots, dark photos, varied skin tones).
   Aim for ≥100 per positive class, ≥200 negatives.
2. `python scripts/calibrate.py scan calib_set --out detections.jsonl` (one slow pass)
3. `python scripts/calibrate.py sweep detections.jsonl` — pick thresholds with
   **zero misses** on the positive sets, then minimize the negative fire rate;
   edit `config.yaml`; re-run `sweep` (instant, no re-inference).
4. Run dry-run on the real group for a few days, compare reports to reality,
   then set `dry_run: false`.

`python scripts/calibrate.py probe photo.jpg` shows raw model output for one image.

Ongoing: every admin override and reported miss should go into the calibration
set; re-sweep monthly. Raw detections for every screening are also stored in
the `detections` table of `nazoratchi.db`.

## Accepted limitations (read this, admins)

- **Post-approval swap succeeds by design.** Anyone can join with a clean
  profile and change photo/bio to porn advertising afterwards; nothing is
  re-scanned after acceptance. (Possible future work: nightly re-scan of
  recent joiners.)
- **Stories are invisible to bots.** The Bot API cannot list or view any
  user's stories; only an MTProto user-account sidecar could, with real
  ToS/ban risk — deliberately not built. The screening pipeline has a seam
  where such a checker could be plugged in later.
- **Hidden or absent profile photos pass the photo check.** The API returns
  an empty list for both "has none" and "hidden by privacy settings" —
  punishing absence would reject every privacy-conscious legitimate user.
  Text checks still apply; the report notes "not scanned".
- **Drawn/anime porn and sticker-censored images evade the detector.** The v2
  classifier backstop catches some; for the rest, detection rests on the
  account's text and links.
- **The fallback path has a window**: directly-added users are inside the
  group for the few seconds screening takes before a kick.
- **The keyword list is probeable** — a patient adversary can iterate bios
  until one passes. Expect decay; update the lists (the Gemini tier softens
  this).
- **Swimwear/gym photos of real users will land in the review queue.** That
  is the designed trade-off for zero tolerance: the admin spends one tap.
- **Single process, single instance** (SQLite + polling). No HA. Don't run
  two copies against the same group.
- **Licensing**: this project is **AGPL-3.0** (see LICENSE). NudeNet's GitHub
  repo is AGPL-3.0 (its PyPI metadata says MIT); publishing this bot's full
  source under AGPL-3.0 satisfies the network-use clause under the strictest
  reading.

## Operations notes

- Startup self-checks verify: models load + test inference, bot is admin with
  invite rights in every guarded chat, admin chat reachable (fatal if not),
  Gemini key present. Problems are alerted, never silently absorbed.
- Every incoming request is persisted to SQLite **before** processing; on
  restart, unresolved screenings resume automatically.
- A queue depth ≥ `queue.flood_alert_depth` triggers a raid alert; requests
  are never dropped, only delayed.
- If Gemini is down, text verdicts fall back to regex-only and reports say so.
- Measured performance: ~43 ms/photo for the full ensemble on an Apple-silicon
  CPU; a 4-worker pool handles a large join flood comfortably.
