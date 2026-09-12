# Day-to-day shortcuts. `make` alone lists them.
LAST ?= 50

# data/prod.db is phony on purpose: every backup/log starts from a fresh pull.
.PHONY: help backup log local data/prod.db test lint fmt deploy

help:
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

backup: data/prod.db  ## pull the production db and files, zip them with the notes -> data/backups/
	uv run python -m family_ea backup --db data/prod.db

log: data/prod.db  ## pull the production db, print the last messages (LAST=50)
	uv run python -m family_ea log --db data/prod.db --last $(LAST)

local: data/prod.db  ## pull production, then make it the local db and files (chat, web)
	rm -f data/family.db data/family.db-wal data/family.db-shm
	cp data/prod.db data/family.db
	mkdir -p data/files && cp -R data/prod-files/. data/files/

data/prod.db:
	uv run python -m family_ea pull

test:  ## run the tests
	uv run pytest -q

lint:  ## ruff check + format check
	uv run ruff check . && uv run ruff format --check .

fmt:  ## ruff fix + format
	uv run ruff check --fix . && uv run ruff format .

deploy:  ## fly deploy, always single machine
	fly deploy --ha=false
