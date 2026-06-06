# Michibiki — 導き

Daily sync service for Mihon manga reading progress.

Two modes:
- **watch** (recommended): Watches for Mihon `.tachibk` backup files (via Syncthing), parses them, searches AniList for unbound titles, and pushes reading progress to AniList. Also optionally populates Suwayomi's library.
- **poll** (legacy): Queries Suwayomi for tracker progress and syncs to AniList.

## How It Works (watch mode)

```
Mihon (phone) ──Syncthing──▶ backup files (.tachibk)
                                    │
                              Michibiki watches /app/backups
                                    │
                          ┌─────────┼─────────┐
                          ▼                   ▼
                      AniList              Suwayomi
                   (sync progress)      (populate library
                                         + bind trackers)
```

- **Backup parser** extracts manga titles, chapter progress, and existing AniList tracker bindings from Mihon's protobuf backup files
- **AniList search** fuzzy-matches unbound manga to AniList media IDs
- **AniList sync** pushes reading progress via `SaveMediaListEntry` mutation
- **Suwayomi population** (optional) searches extensions, adds manga to library, and binds trackers — enabling the full Suwayomi→AniList pipeline

## Prerequisites

1. **Mihon** on Android with scheduled auto-backups enabled (Settings → Data and storage → Backups → every 24h)
2. **Syncthing** syncing Mihon's backup folder to the server (phone → server, Send Only recommended)
3. **Suwayomi-Server** running (for library population mode — optional if only using AniList sync)
4. **AniList OAuth token** — create a client at [AniList Developer Settings](https://anilist.co/settings/developer), then visit:
   ```
   https://anilist.co/api/v2/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=token
   ```
   Copy the token from the redirect URL.

### Suwayomi Extensions (for library population)

If using `POPULATE_SUWAYOMI=true`, install source extensions in Suwayomi's WebUI:

1. Open **Suwayomi WebUI** → Settings → Browse → Extension repos
2. Add the Keiyoushi extension repo:
   ```
   https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json
   ```
3. Go to Extensions tab → install the sources your Mihon uses (e.g., Bato.to, MangaDex, Comick)

Without extensions installed, Suwayomi population will fail silently — only the AniList sync is essential.

## Quick Start

```bash
# Clone
git clone https://github.com/completeBeta/Michibiki.git
cd Michibiki

# Configure
cp .env.example .env
# Edit .env — paste your AniList token

# Create backup directory for Syncthing to drop files into
mkdir -p backups

# Dry run first
DRY_RUN=true MODE=watch docker compose up michibiki
docker compose logs michibiki

# Live run
# Set DRY_RUN=false in .env, then:
docker compose up -d
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `SUWAYOMI_URL` | `http://suwayomi:4567/api/graphql` | Suwayomi GraphQL endpoint |
| `ANILIST_TOKEN` | *(required)* | AniList OAuth2 access token |
| `POLL_INTERVAL_SECONDS` | `86400` | Seconds between backup directory scans |
| `DRY_RUN` | `false` | Log updates without pushing to AniList |
| `MODE` | `watch` | `watch` for backup watcher, `poll` for Suwayomi→AniList |
| `BACKUP_DIR` | `/app/backups` | Directory watched for `.tachibk` files |
| `POPULATE_SUWAYOMI` | `true` | Add manga to Suwayomi + bind AniList trackers |
| `CLEAR_SUWAYOMI_FIRST` | `false` | Remove all manga from Suwayomi before populating |

## Volume Mounts

| Host path | Container path | Purpose |
|---|---|---|
| `/opt/docker/michibiki/data` | `/app/data` | SQLite state store |
| `/home/syncthing/data/mihon-backups` | `/app/backups` | Mihon `.tachibk` files from Syncthing |
| `/opt/docker/suwayomi/data` | `/home/suwayomi/.local/share/Tachidesk` | Suwayomi library + extensions |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

## Project Structure

```
src/
├── main.py                # Entrypoint — selects watch/poll mode
├── config.py              # Env var loading
├── backup_parser.py       # .tachibk protobuf parser
├── anilist_search.py      # AniList title→media ID search
├── bakumon.py             # Orchestrator — parse→sync→populate
├── suwayomi_populator.py  # Suwayomi library population + tracker binding
├── suwayomi.py            # Suwayomi GraphQL client (poll mode)
├── anilist.py             # AniList GraphQL client
├── state.py               # SQLite state store
├── sync.py                # Sync engine (poll mode)
└── mihon_backup.proto     # Protobuf schema for Mihon backup format

tests/
```

## Safeguards

- **Syncthing phone → server only**: Set the phone's folder to Send Only. The server never writes back — your Mihon library is the authoritative source.
- **DRY_RUN=true by default**: First run logs what would change without touching AniList.
- **Read-only backup mount**: Michibiki only reads `.tachibk` files, never modifies them.
- **No overwrites**: AniList `SaveMediaListEntry` is an upsert — it updates reading progress and status, never deletes entries.
- **Suwayomi population is optional**: Set `POPULATE_SUWAYOMI=false` if you only want AniList sync.
