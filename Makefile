# Michibiki — 導き
#
# Daily sync service: queries Suwayomi for Mihon reading progress,
# diffs against last-known state, and pushes updates to AniList.
#
# Runs once per day. Set DRY_RUN=true for a first-run preview.

include .env

.PHONY: build up down logs test test-unit shell

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f michibiki

test:
	python -m pytest tests/ -v

test-unit:
	python -m pytest tests/ -v --ignore=tests/test_integration.py

shell:
	docker compose exec michibiki /bin/bash
