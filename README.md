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
- **Suwayomi population** (experimental) searches extensions, adds manga to library, and binds trackers. **Currently WIP** — the population strategy and data movement pipeline will be standardized in a future update. For now, set `POPULATE_SUWAYOMI=false` and use only the AniList sync.

## Prerequisites

1. **Mihon** on Android with scheduled auto-backups enabled (Settings → Data and storage → Backups → every 24h)
2. **Syncthing** syncing Mihon's backup folder to the server (phone → server, Send Only recommended)
3. **Suwayomi-Server** running (for library population mode — optional if only using AniList sync)
4. **AniList OAuth token** — create a client at [AniList Developer Settings](https://anilist.co/settings/developer), then visit:
   ```
   https://anilist.co/api/v2/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=token
   ```
   Copy the token from the redirect URL.

### Suwayomi Extensions (for library population — WIP)

> ⚠️ Suwayomi population is experimental and not yet production-ready. This section is for future reference.

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
| `POPULATE_SUWAYOMI` | `false` | (WIP) Add manga to Suwayomi + bind AniList trackers |
| `CLEAR_SUWAYOMI_FIRST` | `false` | Remove all manga from Suwayomi before populating |
| `SUWAYOMI_DOWNLOADS_DIR` | *(auto)* | Where Suwayomi writes downloaded CBZ files. Defaults inside Suwayomi's data volume. Set to any host path (local folder, CIFS/NFS mount) to redirect downloads — e.g. `/mnt/nas/manga` |

## Volume Mounts

| Host path | Container path | Purpose |
|---|---|---|
| `/opt/docker/michibiki/data` | `/app/data` | SQLite state store |
| `/home/syncthing/data/mihon-backups` | `/app/backups` | Mihon `.tachibk` files from Syncthing |
| `/opt/docker/suwayomi/data` | `/home/suwayomi/.local/share/Tachidesk` | Suwayomi library + extensions |
| `$SUWAYOMI_DOWNLOADS_DIR` | `/home/suwayomi/.local/share/Tachidesk/downloads` | CBZ chapter downloads (overlays parent mount). Set in `.env` — empty = default path inside data volume |

### Download storage

By default, Suwayomi stores downloaded CBZ files inside its data volume. To redirect downloads to a different location (local folder, network mount, etc.), set `SUWAYOMI_DOWNLOADS_DIR` in `.env`:

```env
# Write downloads to a local folder
SUWAYOMI_DOWNLOADS_DIR=/srv/manga

# Or a network mount
SUWAYOMI_DOWNLOADS_DIR=/mnt/nas/manga
```

The path must exist on the host before running `docker compose up`. Docker overlays the more specific `/downloads` bind mount on top of the parent `/Tachidesk` mount — Suwayomi's config and database stay in the data volume, only downloads are redirected.

### Title overrides

Some manga titles confuse AniList's search ranking — popular series often match spinoffs instead of the main series. Edit `data/title_overrides.json` to force the correct AniList media ID. Keys are lowercase, case-insensitive. Restart the container after editing (no rebuild needed):

```json
{
    "_comment": "Map backup titles to AniList media IDs.",
    "classroom of the elite": 96798
}
```

## Downloading Manga

Suwayomi auto-downloads **new chapters** as they release. To grab the **backlog** (existing chapters), use the built-in download CLI:

```bash
# Dry run first — see what would download
docker exec michibiki python -m src.download "Omniscient Reader" --all --dry-run

# Download all chapters (30 per batch, 3-min gaps — safe for Bato.to)
docker exec michibiki python -m src.download "Omniscient Reader" --all

# Download a range
docker exec michibiki python -m src.download "One Piece" --range 1-100

# First N chapters only
docker exec michibiki python -m src.download "Solo Leveling" --limit 50

# By Suwayomi manga ID (faster, no name lookup)
docker exec michibiki python -m src.download --id 42 --all

# Custom batch size and delay
docker exec michibiki python -m src.download "Omniscient Reader" --all --batch-size 20 --delay 120
```

The title is fuzzy-matched against your Suwayomi library. If multiple series match, it lists them — use a more specific title or `--id`.

Downloads land in `SUWAYOMI_DOWNLOADS_DIR` (defaults to Suwayomi's data volume, configurable in `.env`).

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
├── download.py            # CLI batch downloader (docker exec ... --all)
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
