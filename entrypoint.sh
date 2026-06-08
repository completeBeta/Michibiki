#!/bin/bash
# Michibiki entrypoint — runs sync service + webui side by side.
set -e

echo "[entrypoint] Starting Michibiki sync service..."
python -m src.main &

echo "[entrypoint] Starting WebUI on port ${WEBUI_PORT:-5001}..."
exec python -m src.webui
