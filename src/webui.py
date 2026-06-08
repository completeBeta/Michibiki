"""Michibiki WebUI — browser-based control panel for Suwayomi downloads.

FastAPI + Jinja2 + HTMX. Runs alongside the sync service.
Designed for Cloudflare Access — no built-in auth (CF handles it).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from src.config import load_config
from src.bakumon import sync_from_backup
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
    yield


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
        "script-src 'self' 'unsafe-inline'; "
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
          chapterCount
          downloadCount
          unreadCount
          sourceId
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
        chapterCount
        downloadCount
        chapters(condition: { mangaId: $id }) {
          nodes {
            id
            name
            chapterNumber
            downloaded
          }
        }
      }
    }
    """, {"id": manga_id})
    return data.get("data", {}).get("manga", None)


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
    """Run a download task in the background, updating _download_tasks."""
    task = _download_tasks.get(task_id)
    if not task:
        return

    try:
        task.status = "running"
        async with httpx.AsyncClient(timeout=30) as client:
            chapters = await _get_chapters(client, manga_id)
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

            # Queue in batches
            chapter_ids = [c["id"] for c in chapters]
            batches = [
                chapter_ids[i : i + batch_size]
                for i in range(0, len(chapter_ids), batch_size)
            ]

            task.total_chapters = len(chapter_ids)
            for i, batch in enumerate(batches, 1):
                await _queue_batch(client, batch)
                task.queued += len(batch)
                if i < len(batches):
                    # Non-blocking sleep — yield to other tasks
                    for _ in range(delay):
                        if task.status == "cancelled":
                            return
                        await asyncio.sleep(1)

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
        from src.config import load_config
        from src.sync import sync_from_backup

        config = load_config()
        _scan_state["message"] = "Searching sources..."

        result = asyncio.run(sync_from_backup(
            backup_path=backup_path,
            config=config,
            populate_suwayomi=True,
            clear_suwayomi_first=False,
            dry_run=False,
        ))

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

    return RedirectResponse(url=f"/manga/{manga_id}?task={task_id}", status_code=303)


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_view(request: Request):
    """HTMX partial — refresh task list."""
    tasks = sorted(_download_tasks.values(), key=lambda t: t.started_at, reverse=True)
    plain_tasks = [_task_to_dict(t) for t in tasks]
    return _render(
        "tasks.html",
        {"request": request, "tasks": plain_tasks},
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
