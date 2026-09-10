# 📡 Teleporter

**A web studio for migrating & indexing Telegram channels, groups, and forum topics.**

Teleporter copies content between Telegram chats using a **true copy** (re-upload from your
account session) — not forwarding — so copied messages carry **no “Forwarded from…” tag**.
It can also **index** any channel or group and produce a detailed HTML/JSON/CSV report, and
for **forum (Topics) destinations** it indexes first, auto-classifies the content, lets you
review the topic plan, then files every message into the right topic.

---

## ✨ Features

| # | Operation | What it does |
|---|-----------|--------------|
| 1 | **Channel → Channel** | Direct copy, everything selected — no indexing |
| 2 | **Channel → Group** | Channel content into a group |
| 3 | **Group → Channel** | Group content into a channel |
| 4 | **Group → Group** | Group-to-group copy |
| 5 | **Channel → Forum Topics** | Index → classify → review plan → copy into topics |
| 6 | **Group → Forum Topics** | Index → classify → review plan → copy into topics |
| 7 | **Index Only** | Scan a chat and generate an HTML report (nothing copied) |

**Content control** — for every job you pick exactly what to copy:
Files/Documents · Photos · Videos · GIFs · Audio · Voice messages · Round videos ·
Text messages · Links only (URL extraction) · Stickers · Polls · Locations · Contacts · All.
Multi-select supported.

**Telegram auth** — connect with an existing **session string** (API ID + API Hash + string),
or **generate a new one** in-app: phone → code → optional 2FA password. After login the UI
shows `Logged in as: Name (@username)`. The session is saved on the server (SQLite) so you
only log in once.

**Reliability**

- ⏸ Pause / ▶ Resume / ✕ Cancel at any time — progress is checkpointed in SQLite
- ↻ Resumes exactly where it stopped (tracks last message id + already-copied set)
- 🚦 FloodWait-aware: sleeps when Telegram asks, then continues
- ⏱ Configurable delay between messages (default 1s)
- 🗂 Forum topics are auto-created in the destination if missing (incl. keyword-based routing)

**Index reports** — self-contained HTML dashboard (type breakdown, monthly activity,
top links, top contributors, largest files, full message index) + JSON & CSV exports.

---

## 🏗 Tech stack

- **Backend:** Python 3.12 · FastAPI · [Pyrofork](https://github.com/pyrogram/pyrofork)
  (maintained Pyrogram fork with full forum-topic support) · aiosqlite (SQLite)
- **Frontend:** vanilla HTML/CSS/JS — zero build step, zero CDN dependencies
- **Deploy:** Docker image → Render, Railway, Fly.io, any VPS

> **Why not Vercel?** Telegram migration needs a *persistent* MTProto connection and
> long-running background jobs. Serverless platforms (Vercel/Netlify functions) kill
> idle processes and can't hold sessions. Use any Docker-based host instead.

---

## 🚀 Quick start (local)

```bash
git clone <your-repo-url> teleporter && cd teleporter
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional: set APP_PASSWORD etc.
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** and follow the wizard.

## 🐳 Docker

```bash
docker compose up -d          # http://localhost:8000
# or plain:
docker build -t teleporter .
docker run -p 8000:8000 -v $(pwd)/data:/data -e APP_PASSWORD=changeme teleporter
```

The SQLite database + index data live in `/data` (mounted volume → survives redeploys).

---

## ☁️ Deploying

### Render
1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo. `render.yaml` configures everything:
   Docker runtime, health check, and a **1 GB persistent disk** at `/data`.
3. Set `APP_PASSWORD` to a strong value (the blueprint auto-generates one — check the
   env vars tab and copy it).

### Railway
1. Push to GitHub → Railway → **New Project → Deploy from repo**.
2. Railway auto-detects the `Dockerfile` and `railway.json`.
3. Add a **Volume** mounted at `/data`, and set env var `APP_PASSWORD`.

### Fly.io
```bash
fly launch --no-deploy        # accept defaults
fly volumes create teleporter_data --size 1
# add to fly.toml: [mounts] source = "teleporter_data", destination = "/data"
fly secrets set APP_PASSWORD=yourpassword
fly deploy
```

### Any VPS
`git clone` → `docker compose up -d` behind your reverse proxy (Caddy/Nginx/Traefik).

---

## 🔐 Security notes — read this

- The session string is **equivalent to your Telegram account**. Teleporter stores it
  in the SQLite DB on the server. Anyone with access to that file has your account.
- **Always set `APP_PASSWORD`** on public deployments. The UI asks for it before use.
- Prefer hosting on a private instance; the app is designed as a **single-user tool**.
- Log out from the UI (**Disconnect**) when done — it deletes the stored session.

## ⚖️ Fair use

Copying via a user session is subject to Telegram's Terms of Service. Move only content
you own or have the right to redistribute, keep the default delay (or higher) for large
jobs, and note that Telegram may restrict accounts that mass-copy aggressively. Channels
with *Restrict saving content* can still be copied this way — respect that flag's intent.

---

## 🧭 Using the app

1. **Pick an operation** (7 cards on the home screen).
2. **Connect your account** — paste a session string, or generate one:
   API ID/Hash from [my.telegram.org](https://my.telegram.org) → API development tools.
3. **Choose source & destination** — search your chat list, or paste an `@username`,
   invite link, or numeric ID. Forum operations validate the destination automatically.
4. **Pick content types** to copy + delay between messages.
5. **Run.** Watch live progress, pause/resume/cancel anytime.
   - Forum jobs pause after indexing and show the **topic plan editor**: rename topics,
     re-assign content types, add keyword routing rules, or skip types entirely.
     Missing topics are created automatically when migration starts.
   - Index jobs finish with an **HTML report** (+ JSON/CSV downloads).
6. **History tab** — every job is kept, resumable, and re-openable.

## ⚙️ Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `APP_PASSWORD` | *(empty)* | Unlock password for the web UI. **Set it on public hosts.** |
| `DATA_DIR` | `./data` | Where SQLite DB lives (`/data` in Docker) |
| `DB_PATH` | `$DATA_DIR/teleporter.db` | Explicit DB file path |
| `DEFAULT_API_ID` / `DEFAULT_API_HASH` | *(empty)* | Pre-fill the login form |
| `DEFAULT_DELAY` | `1.0` | Default seconds between copied messages |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind address |

## 📁 Project layout

```
app/
├── main.py        # FastAPI entrypoint + static serving
├── api.py         # all HTTP routes (auth, chats, jobs, reports)
├── engine.py      # indexing + migration engine (pause/resume, flood-safe)
├── classify.py    # message typing, filters, keyword/topic classification
├── report.py      # HTML/JSON/CSV index reports
├── tg.py          # Telegram client manager (session strings, login flow, topics)
├── db.py          # SQLite layer (jobs, index rows, copied set, logs)
├── config.py      # env configuration
└── static/        # frontend (index.html, app.js, style.css)
Dockerfile · docker-compose.yml · render.yaml · railway.json
```

## 📜 License

MIT — use it, fork it, ship it.
