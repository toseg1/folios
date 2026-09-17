.PHONY: setup check up down metabase-up metabase-down install-launchd

# No --project-directory: leaving it unset makes relative paths inside the
# compose file (./initdb) resolve against docker/, where it actually lives.
# --env-file still points at the repo-root .env explicitly.
COMPOSE = docker compose -p folios --env-file .env -f docker/compose.yml

# A separate Compose project on purpose — see metabase/compose.yml.
METABASE_COMPOSE = docker compose -p folios-metabase --env-file .env -f metabase/compose.yml

setup:
	pip install -r requirements.txt
	# stockdex must be installed with --no-deps: its own metadata pins
	# curl_cffi==0.12.0, which conflicts with yfinance's >=0.15 — see the
	# comment above stockdex's transitive deps in requirements.txt.
	pip install --no-deps stockdex==1.2.7

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

# Renders launchd/com.folios.sync.plist.template with this repo's
# absolute path and installs it — but does NOT load it. `launchctl load`
# is the one step left for you to run by hand (build-plan §7 item 7):
#   launchctl load ~/Library/LaunchAgents/com.folios.sync.plist
install-launchd:
	mkdir -p logs
	sed 's|__FOLIOS_REPO__|$(CURDIR)|g' launchd/com.folios.sync.plist.template \
		> ~/Library/LaunchAgents/com.folios.sync.plist
	@echo "Installed. Now run:"
	@echo "  launchctl load ~/Library/LaunchAgents/com.folios.sync.plist"
