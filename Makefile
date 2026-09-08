.PHONY: help up down restart logs build rebuild shell test venv image import-red backup

help:           ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t20

up:             ## Build if needed and start the bot in the background
	docker compose up -d --build

down:           ## Stop and remove the container
	docker compose down

restart:        ## Restart the bot (also re-runs the yt-dlp auto-update)
	docker compose restart bot

logs:           ## Follow the logs
	docker compose logs -f bot

build:          ## Build the image
	docker compose build

rebuild:        ## Rebuild from scratch with fresh base layers and restart
	docker compose build --pull --no-cache && docker compose up -d

shell:          ## Open a shell inside the running container
	docker compose exec bot bash

test:           ## Run the test suite locally (needs `make venv` first)
	PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -t . -v

venv:           ## Create a local Python 3.12 dev environment
	uv venv --python 3.12 .venv && VIRTUAL_ENV=.venv uv pip install -r requirements.txt

image:          ## Build the image for a Docker UI (Arcane/Portainer) to run
	docker build -t discord-bot:latest .

import-red:     ## Preview a Red import: make import-red SRC=<path> [ARGS=--apply]
	PYTHONPATH=. .venv/bin/python -m bot.tools.import_red $(SRC) $(ARGS)

backup:         ## Snapshot the database next to it, timestamped
	@cp data/bot.db "data/bot-$$(date +%Y%m%d-%H%M%S).db" && echo "Backed up to data/"
