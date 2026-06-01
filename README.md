# Michibiki — 導き

Daily sync service: queries [Suwayomi](https://github.com/Suwayomi/Suwayomi-Server) for Mihon reading progress, diffs against last-known state, and pushes updates to AniList.

Runs once every 24 hours. Set `DRY_RUN=true` for a safe first-run preview.

## How It Works

```
Mihon (phone) ──tracker──▶ Suwayomi (server) ──GraphQL──▶ Michibiki ──GraphQL──▶ AniList
```

- **Suwayomi** stores reading progress and AniList track records (media ID + last chapter read)
- **Michibiki** polls Suwayomi's GraphQL API, diffs against local SQLite state, and pushes changes to AniList's `SaveMediaListEntry` mutation
- No fuzzy title matching needed — Suwayomi's track records already map manga to AniList media IDs

## Prerequisites

1. **Suwayomi-Server running** with Mihon synced as a tracker
2. **AniList tracker linked** — in Suwayomi's WebUI, bind each manga to its AniList entry
3. **AniList OAuth token** — get from [AniList API v2 OAuth](https://anilist.co/api/v2/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=token)

## Quick Start

```bash
# Clone
git clone https://github.com/completeBeta/Michibiki.git
cd Michibiki

# Configure
cp .env.example .env
# Edit .env — paste your AniList token

# First run (dry run — no changes made)
DRY_RUN=true docker compose up michibiki
# Check logs to preview what would be updated
docker compose logs michibiki

# Live run
# Set DRY_RUN=false in .env, then:
docker compose up -d
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SUWAYOMI_URL` | `http://suwayomi:4567/api/graphql` | Suwayomi GraphQL endpoint |
| `ANILIST_TOKEN` | *(required)* | AniList OAuth2 access token |
| `POLL_INTERVAL_SECONDS` | `86400` | Seconds between sync runs |
| `DRY_RUN` | `false` | Log updates without pushing to AniList |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

## Project Structure

```
src/
├── main.py       # Entrypoint — daily loop
├── config.py     # Env var loading
├── suwayomi.py   # Suwayomi GraphQL client
├── anilist.py    # AniList GraphQL client
├── state.py      # SQLite state store
└── sync.py       # Sync orchestration

tests/
├── test_config.py
├── test_suwayomi.py
├── test_anilist.py
├── test_state.py
└── test_sync.py
```
