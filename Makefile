.PHONY: setup check up down metabase-up metabase-down

# No --project-directory: leaving it unset makes relative paths inside the
# compose file (./initdb) resolve against docker/, where it actually lives.
# --env-file still points at the repo-root .env explicitly.
COMPOSE = docker compose -p folios --env-file .env -f docker/compose.yml

# A separate Compose project on purpose — see metabase/compose.yml.
METABASE_COMPOSE = docker compose -p folios-metabase --env-file .env -f metabase/compose.yml

setup:
	pip install -r requirements.txt

check:
	ruff check .
	pytest --cov=folios

# The folios Postgres warehouse only. No Metabase — bring your own, or see
# metabase-up below for the optional bundled one.
up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

# Optional convenience Metabase, for anyone who doesn't already run one.
metabase-up:
	$(METABASE_COMPOSE) up -d

metabase-down:
	$(METABASE_COMPOSE) down
