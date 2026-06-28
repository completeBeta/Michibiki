"""Michibiki WebUI — browser-based control panel for Suwayomi downloads.

FastAPI + Jinja2 + HTMX. Runs alongside the sync service.
Designed for Cloudflare Access — no built-in auth (CF handles it).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from starlette.background import BackgroundTask

from src.config import load_config
from src.bakumon import sync_from_backup
from src.cleanup import delete_manga_downloads, run_cleanup_daily
from src.download import (
    _find_manga,
    _get_chapters,
    _queue_batch,
)

log = logging.getLogger("michibiki.webui")

# Scan progress state (shared between /api/rescan and /api/status polling)
_scan_state: dict[str, Any] = {
    "running": False, "status": "", "message": "",
    "added": 0, "bound": 0, "skipped": 0, "failed": 0, "total": 0,
}

SUWAYOMI_URL = os.getenv("SUWAYOMI_URL", "http://suwayomi:4567/api/graphql")
WEBUI_PORT = int(os.getenv("WEBUI_PORT", "5001"))
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "/downloads")

# ── Download task tracker (in-memory, lost on restart) ──────────────

@dataclass
class DownloadTask:
    task_id: str
    manga_title: str
    total_chapters: int
    queued: int = 0
    status: str = "pending"  # pending | running | done | failed
    dry_run: bool = False
    error: str | None = None
    started_at: float = field(default_factory=time.time)

_download_tasks: dict[str, DownloadTask] = {}

# ── App setup ───────────────────────────────────────────────────────

env = Environment(loader=FileSystemLoader("/app/templates"))


def _render(name: str, context: dict) -> HTMLResponse:
    """Render a Jinja2 template to an HTML response."""
    template = env.get_template(name)
    return HTMLResponse(template.render(**context))


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("WebUI starting on port %d", WEBUI_PORT)
    # Start auto-cleanup background task (14-day TTL)
    cleanup_task = asyncio.create_task(run_cleanup_daily())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Michibiki",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# ── Security middleware ─────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


# ── GraphQL helpers ─────────────────────────────────────────────────

async def _graphql(query: str, variables: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            SUWAYOMI_URL,
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        return resp.json()


async def _get_library() -> list[dict[str, Any]]:
    """Get all manga in Suwayomi library with chapter counts."""
    data = await _graphql("""
    query {
      mangas(condition: { inLibrary: true }) {
        nodes {
          id
          title
          downloadCount
          unreadCount
          sourceId
          chaptersAge
        }
      }
    }
    """)
    return data.get("data", {}).get("mangas", {}).get("nodes", [])


async def _get_manga_detail(manga_id: int) -> dict | None:
    data = await _graphql("""
    query($id: Int!) {
      manga(id: $id) {
        id
        title
        downloadCount
        unreadCount
        chapters {
          nodes {
            id
            name
            chapterNumber
            isDownloaded
            pageCount
          }
        }
      }
    }
    """, {"id": manga_id})
    return data.get("data", {}).get("manga", None)


# ── Zip download helpers ────────────────────────────────────────────

_RE_FS_ILLEGAL = re.compile(r'[<>:"/\\|?*]')


def _sanitize_fs(name: str) -> str:
    """Strip characters illegal in filesystem names."""
    return _RE_FS_ILLEGAL.sub('', name).strip()


def _find_manga_dir(manga_title: str) -> Path | None:
    """Find the download directory for a manga by title.

    Suwayomi stores downloads as:
      /downloads/{manga_title}/{chapter_name}/
    or sometimes:
      /downloads/{source_id}/{manga_title}/{chapter_name}/

    This searches one level deep, then two, with fuzzy matching.
    """
    base = Path(DOWNLOADS_DIR)
    if not base.exists():
        log.warning("Downloads dir %s does not exist", DOWNLOADS_DIR)
        return None

    sanitized = _sanitize_fs(manga_title)
    sanitized_lower = sanitized.lower()

    # Pass 1 — direct match at top level
    candidate = base / sanitized
    if candidate.is_dir():
        return candidate

    # Pass 2 — case-insensitive top-level
    try:
        for d in base.iterdir():
            if d.is_dir() and d.name.lower() == sanitized_lower:
                return d
    except PermissionError:
        pass

    # Pass 3 — contains-match at top level (handles source-prefixed names)
    try:
        for d in base.iterdir():
            if d.is_dir() and sanitized_lower in d.name.lower():
                return d
    except PermissionError:
        pass

    # Pass 4 — inside /downloads/mangas/ one level (source name / title)
    mangas_dir = base / "mangas"
    if mangas_dir.is_dir():
        try:
            for source_dir in mangas_dir.iterdir():
                if not source_dir.is_dir():
                    continue
                for d in source_dir.iterdir():
                    if d.is_dir() and d.name.lower() == sanitized_lower:
                        return d
        except PermissionError:
            pass

    # Pass 5 — two levels deep from /downloads/ (source_id / title)
    try:
        for source_dir in base.iterdir():
            if not source_dir.is_dir():
                continue
            for d in source_dir.iterdir():
                if d.is_dir() and d.name.lower() == sanitized_lower:
                    return d
    except PermissionError:
        pass

    return None


def _find_chapter_dir(manga_dir: Path, chapter_name: str) -> Path | None:
    """Find a specific chapter directory within a manga dir.

    Suwayomi names chapter dirs by chapter name (e.g. "Chapter 1").
    Falls back to fuzzy matching for edge cases.
    """
    sanitized = _sanitize_fs(chapter_name)
    sanitized_lower = sanitized.lower()

    # Exact match
    candidate = manga_dir / sanitized
    if candidate.is_dir():
        return candidate

    # Case-insensitive
    try:
        for d in manga_dir.iterdir():
            if d.is_dir() and d.name.lower() == sanitized_lower:
                return d
    except PermissionError:
        pass

    # Contains match
    try:
        for d in manga_dir.iterdir():
            if d.is_dir() and sanitized_lower in d.name.lower():
                return d
    except PermissionError:
        pass

    return None


def _build_chapter_zip(
    output_path: str,
    chapters: list[dict],
    manga_dir: Path,
    manga_title: str,
) -> tuple[int, int]:
    """Build a zip of downloaded chapters. Runs in thread pool.

    Returns (chapters_added, pages_added).
    """
    safe_title = _sanitize_fs(manga_title)
    chapters_added = 0
    pages_added = 0

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for ch in chapters:
            ch_name = ch.get("name", f"Chapter {ch.get('chapterNumber', '?')}")
            ch_num = ch.get("chapterNumber") or 0

            ch_dir = _find_chapter_dir(manga_dir, ch_name)
            if not ch_dir:
                log.warning("Chapter dir not found: %s / %s", manga_title, ch_name)
                continue

            # Folder name inside zip: "Ch 001 - Chapter Name"
            ch_safe = _sanitize_fs(ch_name)
            zip_prefix = f"Ch {float(ch_num):06.1f} - {ch_safe}" if ch_num else ch_safe

            page_files = sorted(
                [p for p in ch_dir.iterdir() if p.is_file()],
                key=lambda p: p.name,
            )
            for page in page_files:
                zf.write(str(page), f"{zip_prefix}/{page.name}")
                pages_added += 1

            chapters_added += 1

    return chapters_added, pages_added


# ── Background download runner ──────────────────────────────────────

async def _run_download(
    task_id: str,
    manga_id: int,
    *,
    all_chapters: bool = False,
    chapter_range: str | None = None,
    dry_run: bool = False,
    batch_size: int = 30,
    delay: int = 180,
    limit: int | None = None,
):
    """Run a download task in the background, updating _download_tasks.
    
    Uses the Governor (WebSocket subscription) to track real download 
    completion instead of blind sleep between batches.
    """
    from src.governor import Governor

    task = _download_tasks.get(task_id)
    if not task:
        return

    try:
        task.status = "running"
        async with httpx.AsyncClient(timeout=30) as client:
            chapters = await _get_chapters(client, manga_id)
            # Filter already-downloaded chapters
            chapters = [c for c in chapters if not c.get("isDownloaded")]
            task.total_chapters = len(chapters)

            # Filter
            if chapter_range:
                try:
                    start, end = chapter_range.split("-")
                    start_c, end_c = float(start), float(end)
                except ValueError:
                    task.status = "failed"
                    task.error = f"Invalid range: {chapter_range}"
                    return
                chapters = [
                    c for c in chapters
                    if start_c <= float(c.get("chapterNumber", 0) or 0) <= end_c
                ]
            if limit:
                chapters = chapters[:limit]

            if not chapters:
                task.status = "done"
                task.queued = 0
                return

            if dry_run:
                task.queued = len(chapters)
                task.status = "done"
                return

            # Queue in batches with real-time governor tracking
            chapter_ids = [c["id"] for c in chapters]
            batches = [
                chapter_ids[i : i + batch_size]
                for i in range(0, len(chapter_ids), batch_size)
            ]

            task.total_chapters = len(chapter_ids)
            governor = Governor()
            try:
                # Connect BEFORE queueing — governor must be alive when downloads start
                await governor.connect()
                for i, batch in enumerate(batches, 1):
                    await _queue_batch(client, batch)
                    task.queued += len(batch)
                    if task.status == "cancelled":
                        return
                    done, failed = await governor.wait_for_batch(
                        batch, timeout_per_chapter=max(delay, len(batch) * 60)
                    )
            finally:
                await governor.disconnect()

            task.status = "done"

    except Exception as e:
        log.error("Download task %s failed: %s", task_id, e)
        task.status = "failed"
        task.error = str(e)


# ── Routes ──────────────────────────────────────────────────────────

def _task_to_dict(t: DownloadTask) -> dict:
    return {
        "task_id": t.task_id,
        "manga_title": t.manga_title,
        "total_chapters": t.total_chapters,
        "queued": t.queued,
        "status": t.status,
        "dry_run": t.dry_run,
        "error": t.error,
    }


@app.post("/api/rescan")
async def rescan_library(request: Request):
    """Trigger a library rescan from the latest backup in the backup directory."""
    import threading

    if _scan_state["running"]:
        return HTMLResponse(
            '<div class="toast warn">Scan already in progress...</div>',
            status_code=409,
        )

    latest = _find_latest_backup()
    if not latest:
        return HTMLResponse(
            '<div class="toast error">No .tachibk backup found. Place one in the backups directory.</div>',
            status_code=404,
        )

    _scan_state.update({
        "running": True, "status": "starting",
        "added": 0, "bound": 0, "skipped": 0, "failed": 0, "total": 0,
        "message": f"Scanning {os.path.basename(latest)}...",
    })

    thread = threading.Thread(
        target=_run_populator_sync, args=(latest,), daemon=True
    )
    thread.start()

    return HTMLResponse(
        '<div class="scan-progress" hx-get="/api/status" '
        'hx-trigger="every 2s" hx-swap="outerHTML">'
        f'<p>⏳ Scanning {os.path.basename(latest)}...</p>'
        '</div>'
    )


@app.get("/api/status")
async def scan_status(request: Request):
    """Get scan progress. HTMX polls this every 2s during a scan."""
    if not _scan_state["running"] and _scan_state["status"] in ("done", "error"):
        cls = "success" if _scan_state["status"] == "done" else "error"
        return HTMLResponse(
            f'<div class="toast {cls}">'
            f'<p>{_scan_state["message"]}</p>'
            f'<p>Refreshing page...</p>'
            f'</div>'
            f'<script>setTimeout(function(){{window.location.reload()}},1500);</script>'
        )
    elif _scan_state["running"]:
        return HTMLResponse(
            '<div class="scan-progress" hx-get="/api/status" '
            'hx-trigger="every 2s" hx-swap="outerHTML">'
            f'<p>⏳ {_scan_state["message"]}</p>'
            '</div>'
        )
    return HTMLResponse('<div></div>')


def _find_latest_backup() -> str | None:
    """Find the most recent .tachibk in the backup directory."""
    backup_dir = os.getenv("BACKUP_DIR", "/app/backups")
    backups = sorted(
        Path(backup_dir).glob("*.tachibk"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not backups:
        return None
    path = str(backups[0])
    log.info("Latest backup: %s (%.1f KB)", path, os.path.getsize(path) / 1024)
    return path


def _run_populator_sync(backup_path: str) -> None:
    """Run populator in a background thread. Updates _scan_state."""
    _scan_state["message"] = "Parsing backup..."
    try:
        from src.backup_parser import parse_backup
        from src.suwayomi_populator import SuwayomiPopulator

        # Parse backup entries
        result = parse_backup(backup_path)
        entries = result.entries
        _scan_state["total"] = len(entries)
        _scan_state["message"] = f"Found {len(entries)} entries, searching sources..."

        # Run populator
        populator = SuwayomiPopulator(SUWAYOMI_URL)
        result = asyncio.run(populator.populate(entries))

        _scan_state["status"] = "done"
        _scan_state.update({
            "added": result.added, "bound": result.bound,
            "skipped": result.skipped, "failed": result.failed,
        })
        _scan_state["message"] = (
            f"Done! {result.added} manga added, "
            f"{result.skipped} skipped, {result.failed} failed"
        )
    except Exception as e:
        log.exception("Rescan failed")
        _scan_state["status"] = "error"
        _scan_state["message"] = f"Error: {e}"
    finally:
        _scan_state["running"] = False


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Main page — library list with search."""
    try:
        manga_list = await _get_library()
    except Exception as e:
        log.error("Failed to fetch library: %s", e)
        manga_list = []

    plain_tasks = [_task_to_dict(t) for t in _download_tasks.values()]
    return _render(
        "index.html",
        {
            "request": request,
            "manga": manga_list,
            "library_empty": len(manga_list) == 0,
            "tasks": plain_tasks,
        },
    )


@app.post("/api/clear")
async def clear_library(request: Request):
    """Remove all manga from Suwayomi library."""
    try:
        data = await _graphql("""
        query {
          mangas(condition: { inLibrary: true }) { nodes { id } }
        }
        """)
        nodes = data.get("data", {}).get("mangas", {}).get("nodes", [])
        count = len(nodes)
        for n in nodes:
            await _graphql("""
            mutation RemoveFromLibrary($input: UpdateMangaInput!) {
              updateManga(input: $input) { clientMutationId }
            }
            """, {"input": {"id": n["id"], "patch": {"inLibrary": False}}})
        log.info("Cleared %d manga from library", count)
        return HTMLResponse(
            f'<div class="toast success"><p>Cleared {count} manga. Refreshing...</p></div>'
            '<script>setTimeout(function(){window.location.reload()},1500);</script>'
        )
    except Exception as e:
        log.exception("Clear failed")
        return HTMLResponse(f'<div class="toast error"><p>Error: {e}</p></div>', status_code=500)


@app.get("/manga/{manga_id}", response_class=HTMLResponse)
async def manga_detail(request: Request, manga_id: int):
    """Manga detail — chapters, download status, controls."""
    try:
        manga = await _get_manga_detail(manga_id)
    except Exception as e:
        log.error("Failed to fetch manga %d: %s", manga_id, e)
        return HTMLResponse(f"<p class='error'>Error: {e}</p>", status_code=500)

    if not manga:
        return HTMLResponse("<p class='error'>Manga not found</p>", status_code=404)

    return _render(
        "manga_detail.html",
        {"request": request, "manga": manga},
    )


@app.post("/download")
async def start_download(
    request: Request,
    manga_id: int = Form(...),
    manga_title: str = Form(...),
    action: str = Form("all"),
    chapter_range: str | None = Form(None),
    limit: str | None = Form(None),
    batch_size: int = Form(30),
    delay: int = Form(180),
    dry_run: bool = Form(False),
):
    """Queue a download task (POST-only)."""
    task_id = uuid.uuid4().hex[:8]
    task = DownloadTask(
        task_id=task_id,
        manga_title=manga_title,
        total_chapters=0,
        dry_run=dry_run,
    )
    _download_tasks[task_id] = task

    all_chapters = action == "all"
    limit_int = int(limit) if limit and limit.strip() else None

    asyncio.create_task(
        _run_download(
            task_id,
            manga_id,
            all_chapters=all_chapters,
            chapter_range=chapter_range if action == "range" else None,
            dry_run=dry_run,
            batch_size=batch_size,
            delay=delay,
            limit=limit_int,
        )
    )

    # Cleanup old tasks (keep last 50)
    if len(_download_tasks) > 50:
        oldest = sorted(_download_tasks.keys())[: len(_download_tasks) - 50]
        for k in oldest:
            del _download_tasks[k]

    label = "DRY RUN" if dry_run else "Download"
    num = limit_int if action == "limit" else (f"range {chapter_range}" if action == "range" else "all")
    return HTMLResponse(
        '<div class="flash flash-info" hx-get="/tasks" hx-trigger="load, every 3s" hx-swap="outerHTML">'
        f'<span class="spinner"></span>'
        f" {label} queued for <strong>{manga_title}</strong>"
        f" ({num}) — "
        f'<a href="/manga/{manga_id}">view detail</a>'
        "</div>"
    )


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_view(request: Request):
    """HTMX partial — refresh task list."""
    tasks = sorted(_download_tasks.values(), key=lambda t: t.started_at, reverse=True)
    plain_tasks = [_task_to_dict(t) for t in tasks]
    return _render(
        "tasks.html",
        {"request": request, "tasks": plain_tasks},
    )


@app.get("/manga/{manga_id}/download")
async def download_manga_zip(request: Request, manga_id: int):
    """Stream a zip of all downloaded chapters for a manga.

    Chapters are organized as folders inside the zip:
        Manga Title.zip
        ├── Ch 001 - Chapter Name/
        │   ├── 001.jpg
        │   └── ...
        └── Ch 002 - Chapter Name/
            └── ...

    Zip is built in a thread pool (non-blocking), streamed via FileResponse,
    and cleaned up automatically after the response completes.
    """
    # 1. Get manga metadata from Suwayomi
    try:
        manga = await _get_manga_detail(manga_id)
    except Exception as e:
        log.error("Failed to fetch manga %d: %s", manga_id, e)
        return HTMLResponse(
            f'<p class="toast error">Error fetching manga: {e}</p>',
            status_code=500,
        )

    if not manga:
        return HTMLResponse(
            '<p class="toast error">Manga not found</p>',
            status_code=404,
        )

    title = manga["title"]
    chapters = manga.get("chapters", {}).get("nodes", [])
    downloaded = [c for c in chapters if c.get("isDownloaded")]

    if not downloaded:
        return HTMLResponse(
            f'<p class="toast warn">No downloaded chapters for <strong>{title}</strong>. '
            f'<a href="/manga/{manga_id}">Download some first</a>.</p>',
            status_code=404,
        )

    # 2. Find the manga's download directory on the filesystem
    manga_dir = _find_manga_dir(title)
    if not manga_dir:
        # Debug: list what directories exist
        base = Path(DOWNLOADS_DIR)
        dirs = (
            [d.name for d in sorted(base.iterdir()) if d.is_dir()][:20]
            if base.exists()
            else []
        )
        log.warning(
            "Manga dir not found for '%s'. Top-level dirs: %s",
            title, dirs,
        )
        return HTMLResponse(
            f'<p class="toast error">Download directory not found for '
            f'<strong>{title}</strong>. Is the downloads volume mounted?</p>',
            status_code=404,
        )

    # 3. Build zip in thread pool (non-blocking)
    tmp_path = tempfile.mktemp(suffix=".zip")
    safe_title = _sanitize_fs(title)

    try:
        chapters_added, pages_added = await asyncio.to_thread(
            _build_chapter_zip,
            tmp_path, downloaded, manga_dir, title,
        )
    except Exception as e:
        log.exception("Zip build failed for %s", title)
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        return HTMLResponse(
            f'<p class="toast error">Failed to build zip: {e}</p>',
            status_code=500,
        )

    if chapters_added == 0:
        os.unlink(tmp_path)
        return HTMLResponse(
            f'<p class="toast warn">Could not locate chapter files for '
            f'<strong>{title}</strong>. {len(downloaded)} chapters marked downloaded '
            f'but none found on disk at <code>{manga_dir}</code>.</p>',
            status_code=404,
        )

    zip_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
    log.info(
        "Serving zip for '%s': %d chapters, %d pages, %.1f MB",
        title, chapters_added, pages_added, zip_size_mb,
    )

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{safe_title}.zip",
        background=BackgroundTask(lambda p=tmp_path: os.unlink(p) if os.path.exists(p) else None),
    )


@app.post("/manga/{manga_id}/delete")
async def delete_downloaded(
    request: Request,
    manga_id: int,
    chapter_ids: str = Form(""),
):
    """Delete downloaded chapters for a manga.

    Accepts comma-separated chapter IDs or 'all' to delete everything.
    Returns HTMX fragment that refreshes the chapter grid.
    """
    try:
        manga = await _get_manga_detail(manga_id)
    except Exception as e:
        log.error("Failed to fetch manga %d: %s", manga_id, e)
        return HTMLResponse(
            '<div class="toast error">Error fetching manga</div>',
            status_code=500,
        )

    if not manga:
        return HTMLResponse(
            '<div class="toast error">Manga not found</div>',
            status_code=404,
        )

    title = manga["title"]
    chapters = manga.get("chapters", {}).get("nodes", [])

    if chapter_ids == "all":
        # Delete all downloaded chapter dirs
        target_chapters = [c for c in chapters if c.get("isDownloaded")]
    else:
        ids = set(int(x.strip()) for x in chapter_ids.split(",") if x.strip().isdigit())
        target_chapters = [c for c in chapters if c["id"] in ids]

    if not target_chapters:
        return HTMLResponse(
            f'<div class="toast warn">No downloaded chapters to delete for <strong>{title}</strong>.</div>',
        )

    # Find manga dir on filesystem
    manga_dir = _find_manga_dir(title)
    if not manga_dir:
        return HTMLResponse(
            f'<div class="toast error">Download directory not found for <strong>{title}</strong>.</div>',
            status_code=404,
        )

    # Delete chapter dirs
    chapter_names = [c["name"] for c in target_chapters]
    deleted = delete_manga_downloads(manga_dir, chapter_names)

    log.info(
        "Deleted %d/%d chapters for '%s' (manga_id=%d)",
        deleted, len(target_chapters), title, manga_id,
    )

    return HTMLResponse(
        f'<div class="toast success" hx-swap-oob="true" id="flash">'
        f'Deleted {deleted} chapter(s) for <strong>{title}</strong>. '
        f'<a href="/manga/{manga_id}">Refresh</a>'
        f'</div>'
        f'<script>setTimeout(function(){{window.location.reload()}},1200);</script>'
    )


# ── Title Override Management ───────────────────────────────────────

OVERRIDES_PATH = Path("/app/data/title_overrides.json")


def _read_overrides() -> dict:
    """Read the title overrides JSON file, returning only int-valued entries."""
    if not OVERRIDES_PATH.exists():
        return {}
    import json
    with open(OVERRIDES_PATH) as f:
        data = json.load(f)
    # Filter out comment/metadata keys — only keep int-valued overrides
    return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}


def _write_overrides(data: dict) -> None:
    """Write the title overrides JSON file."""
    import json
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OVERRIDES_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


@app.get("/overrides", response_class=HTMLResponse)
async def overrides_page(request: Request):
    """Title overrides management page."""
    overrides = _read_overrides()
    return _render("overrides.html", {
        "request": request,
        "overrides": overrides,
        "count": len(overrides),
    })


@app.post("/overrides/add")
async def add_override(
    request: Request,
    title: str = Form(...),
    anilist_id: int = Form(...),
):
    """Add a new title override."""
    if not title.strip():
        return HTMLResponse(
            '<div class="toast error">Title is required.</div>',
            status_code=400,
        )
    overrides = _read_overrides()
    key = title.strip().lower()
    overrides[key] = anilist_id
    _write_overrides(overrides)
    log.info("Added title override: '%s' -> ID %d", title.strip(), anilist_id)
    return HTMLResponse(
        '<div class="toast success">'
        f'Added override: <strong>{title.strip()}</strong> -> ID {anilist_id}. '
        'Changes apply on next sync (no restart needed). '
        '<a href="/overrides">Refresh</a>'
        '</div>'
    )


@app.post("/overrides/delete")
async def delete_override(
    request: Request,
    key: str = Form(...),
):
    """Delete a title override by its simplified key."""
    overrides = _read_overrides()
    if key not in overrides:
        return HTMLResponse(
            '<div class="toast error">Override not found.</div>',
            status_code=404,
        )
    removed_id = overrides.pop(key)
    _write_overrides(overrides)
    log.info("Removed title override: '%s' (was ID %d)", key, removed_id)
    return HTMLResponse(
        '<div class="toast success">'
        f'Removed override: <strong>{key}</strong> (was ID {removed_id}). '
        '<a href="/overrides">Refresh</a>'
        '</div>'
    )


@app.get("/api/anilist/search")
async def anilist_search_webui(request: Request, q: str = ""):
    """Search AniList for manga IDs matching a title. Used by overrides page."""
    if not q.strip():
        return HTMLResponse('<div class="toast warn">Enter a title to search.</div>')
    token = os.getenv("ANILIST_TOKEN")
    if not token:
        return HTMLResponse('<div class="toast error">ANILIST_TOKEN not set.</div>', status_code=500)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://graphql.anilist.co",
                json={
                    "query": """
                    query($search: String) {
                      Page(page: 1, perPage: 10) {
                        media(search: $search, type: MANGA, format_not_in: [NOVEL]) {
                          id
                          title { romaji english }
                          format
                          status
                          chapters
                          volumes
                        }
                      }
                    }
                    """,
                    "variables": {"search": q.strip()},
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("data", {}).get("Page", {}).get("media", []) or []
    except Exception as e:
        log.error("AniList search from WebUI failed: %s", e)
        return HTMLResponse(
            f'<div class="toast error">Search failed: {e}</div>',
            status_code=500,
        )

    if not results:
        return HTMLResponse('<div class="toast warn">No results found.</div>')

    rows = []
    for r in results:
        title = r.get("title", {}).get("romaji") or r.get("title", {}).get("english") or "?"
        fmt = r.get("format", "?")
        ch = r.get("chapters") or "?"
        vol = r.get("volumes") or "?"
        escaped_title = title.replace("'", "\\'")
        rows.append(
            '<div class="search-result">'
            f'<span class="result-title">{title}</span>'
            f'<span class="result-meta">{fmt} | ch:{ch} vol:{vol}</span>'
            f'<button class="btn btn-sm" onclick="'
            f"document.getElementById('override-id').value={r['id']};"
            f"document.getElementById('override-title').value='{escaped_title}'"
            f'">Use ID {r["id"]}</button>'
            '</div>'
        )
    return HTMLResponse(
        f'<div class="search-results">{"".join(rows)}</div>'
    )


# ── CLI entrypoint ──────────────────────────────────────────────────

def main():
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=WEBUI_PORT, log_level="info")


if __name__ == "__main__":
    main()
