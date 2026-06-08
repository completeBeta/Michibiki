"""Download Governor — real-time batch tracking via Suwayomi WebSocket subscription.

B1 approach: Subscribe to downloadStatusChanged, track batch completion,
replace blind time.sleep() with actual download-completion awareness.

Protocol: graphql-transport-ws (https://github.com/enisdenjo/graphql-ws)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("michibiki.governor")

SUWAYOMI_WS_URL = os.getenv("SUWAYOMI_WS_URL", "ws://suwayomi:4567/api/graphql")
SUWAYOMI_HTTP_URL = os.getenv("SUWAYOMI_URL", "http://suwayomi:4567/api/graphql")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "/downloads")

GQL_CONNECTION_INIT = "connection_init"
GQL_CONNECTION_ACK = "connection_ack"
GQL_SUBSCRIBE = "subscribe"
GQL_NEXT = "next"
GQL_COMPLETE = "complete"
GQL_ERROR = "error"
GQL_PING = "ping"
GQL_PONG = "pong"

TERMINAL_STATES = {"FINISHED", "ERROR"}

DOWNLOAD_SUBSCRIPTION = """
subscription {
  downloadStatusChanged(input: {maxUpdates: 50}) {
    state
    updates { type download { chapter { id } manga { id } state progress } }
    initial { chapter { id } manga { id } state progress }
  }
}
"""


class Governor:
    """WebSocket subscriber for real-time download completion tracking."""

    def __init__(self, ws_url: str = SUWAYOMI_WS_URL):
        self.ws_url = ws_url
        self._ws: Any = None
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._chapter_states: dict[int, str] = {}
        self._batch_events: dict[int, asyncio.Event] = {}
        self._batch_chapter_ids: set[int] = set()
        self._receiver_task: asyncio.Task | None = None

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._connected:
                return
            import websockets

            class KeepaliveProtocol(websockets.WebSocketClientProtocol):
                """Override to ignore server-side close — keep listening for download events."""
                pass

            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            )
            await self._ws.send(json.dumps({"type": GQL_CONNECTION_INIT}))
            msg = json.loads(await self._ws.recv())
            if msg.get("type") != GQL_CONNECTION_ACK:
                raise RuntimeError(f"Expected connection_ack, got {msg}")
            await self._ws.send(json.dumps({
                "type": GQL_SUBSCRIBE, "id": "gov-1",
                "payload": {"query": DOWNLOAD_SUBSCRIPTION},
            }))
            self._connected = True
            log.info("Governor connected — subscribed to downloadStatusChanged")
            self._receiver_task = asyncio.create_task(self._receiver_loop())

    async def disconnect(self) -> None:
        self._connected = False
        if self._receiver_task:
            self._receiver_task.cancel()
            self._receiver_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _receiver_loop(self) -> None:
        """Continuously receive and dispatch WebSocket messages."""
        try:
            while self._connected and self._ws:
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    # No messages in 60s — send ping to keep alive
                    if self._connected and self._ws:
                        try:
                            await self._ws.send(json.dumps({"type": GQL_PING}))
                        except Exception:
                            break
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")
                if msg_type == GQL_NEXT:
                    self._process_message(msg)
                elif msg_type == GQL_PING:
                    await self._ws.send(json.dumps({"type": GQL_PONG}))
                elif msg_type == GQL_COMPLETE:
                    # Server finished the subscription stream.
                    # Reconnect immediately — new downloads will trigger new events.
                    log.debug("Server completed subscription stream — reconnecting...")
                    await self._ws.close()
                    self._ws = None
                    await asyncio.sleep(1)
                    try:
                        self._ws = await websockets.connect(
                            self.ws_url,
                            ping_interval=30,
                            ping_timeout=10,
                        )
                        await self._ws.send(json.dumps({"type": GQL_CONNECTION_INIT}))
                        ack = json.loads(await self._ws.recv())
                        if ack.get("type") == GQL_CONNECTION_ACK:
                            await self._ws.send(json.dumps({
                                "type": GQL_SUBSCRIBE, "id": "gov-1",
                                "payload": {"query": DOWNLOAD_SUBSCRIPTION},
                            }))
                            log.debug("Governor reconnected")
                    except Exception as e:
                        log.warning("Governor reconnect failed: %s", e)
                        break
                elif msg_type == GQL_ERROR:
                    log.error("Subscription error: %s", msg.get("payload"))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self._connected:
                log.error("Receiver loop error: %s", e)
        finally:
            self._connected = False

    def _process_message(self, msg: dict) -> None:
        payload = msg.get("payload", {}).get("data", {}).get("downloadStatusChanged")
        if not payload:
            return
        for item in payload.get("initial") or []:
            ch = item.get("chapter")
            cid = ch.get("id") if ch else None
            if cid is not None:
                self._chapter_states[cid] = item.get("state", "?")
        for update in payload.get("updates", []):
            d = update.get("download", {})
            ch = d.get("chapter")
            cid = ch.get("id") if ch else None
            state = d.get("state")
            if cid is not None and state:
                self._chapter_states[cid] = state
                if cid in self._batch_chapter_ids and state in TERMINAL_STATES:
                    ev = self._batch_events.get(cid)
                    if ev and not ev.is_set():
                        ev.set()

    async def wait_for_batch(
        self, chapter_ids: list[int], timeout_per_chapter: int = 600
    ) -> tuple[list[int], list[int]]:
        """Wait for all chapters to reach terminal state. Returns (done, failed)."""
        events: dict[int, asyncio.Event] = {}
        for cid in chapter_ids:
            self._batch_chapter_ids.add(cid)
            ev = asyncio.Event()
            if self._chapter_states.get(cid) in TERMINAL_STATES:
                ev.set()
            events[cid] = ev
        self._batch_events.update(events)

        log.info("Waiting for %d chapters (timeout=%ds)...", len(chapter_ids), timeout_per_chapter)

        done, failed = [], []
        try:
            # Use asyncio.gather with explicit tasks — asyncio.wait() needs Tasks in 3.11+
            tasks = {
                asyncio.create_task(ev.wait()): cid
                for cid, ev in events.items()
            }
            if tasks:
                finished, pending = await asyncio.wait(
                    tasks.keys(),
                    timeout=timeout_per_chapter,
                    return_when=asyncio.ALL_COMPLETED,
                )
                for t in pending:
                    t.cancel()

            for cid in chapter_ids:
                state = self._chapter_states.get(cid, "UNKNOWN")
                (done if state == "FINISHED" else failed).append(cid)
            return done, failed
        finally:
            for cid in chapter_ids:
                self._batch_chapter_ids.discard(cid)
                self._batch_events.pop(cid, None)

    async def organize_downloads(self, manga_title: str, chapter_ids: list[int]) -> int:
        """Move completed CBZs into /downloads/<Manga Title>/."""
        dest_dir = Path(DOWNLOADS_DIR) / manga_title
        dest_dir.mkdir(parents=True, exist_ok=True)
        organized = 0
        async with httpx.AsyncClient(timeout=15) as client:
            for cid in chapter_ids:
                try:
                    resp = await client.post(SUWAYOMI_HTTP_URL, json={
                        "query": "query($id: Int!) { chapter(id: $id) { name chapterNumber isDownloaded } }",
                        "variables": {"id": cid},
                    })
                    resp.raise_for_status()
                    ch = resp.json().get("data", {}).get("chapter", {})
                    if not ch or not ch.get("isDownloaded"):
                        continue
                    ch_num = ch.get("chapterNumber") or "?"
                    ch_name = ch.get("name", f"Ch. {ch_num}")
                    source_dir = Path(DOWNLOADS_DIR)
                    patterns = [f"*Ch. {ch_num}*.cbz", f"*Chapter {ch_num}*.cbz",
                                f"*Ch.{ch_num}*.cbz", f"*{ch_num:04.0f}*.cbz"]
                    found = None
                    for pat in patterns:
                        matches = list(source_dir.glob(pat))
                        if matches:
                            found = max(matches, key=lambda p: p.stat().st_mtime)
                            break
                    if not found:
                        recent = [p for p in source_dir.glob("*.cbz") if time.time() - p.stat().st_mtime < 300]
                        if recent:
                            found = max(recent, key=lambda p: p.stat().st_mtime)
                    if found:
                        safe = "".join(c for c in f"Ch. {ch_num} - {ch_name}.cbz" if c.isprintable()).replace("/", "-")
                        shutil.move(str(found), str(dest_dir / safe))
                        organized += 1
                except Exception as e:
                    log.error("Organize chapter %d failed: %s", cid, e)
        return organized
