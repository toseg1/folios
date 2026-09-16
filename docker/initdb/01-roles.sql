-- Runs once, on first container init, against the `folios` database
-- (POSTGRES_DB) as the Postgres superuser. Passwords come from the
-- container environment — see docker/compose.yml and .env.example.
--
-- Schema-level grants for metabase_ro (GRANT USAGE ON SCHEMA marts,
-- ALTER DEFAULT PRIVILEGES ...) do NOT belong here: schema `marts` does
-- not exist yet at this point, it is created later by
-- migrations/001_schemas_and_core.sql. Those grants land in
-- build-plan step 10 (chore: metabase connection) instead.
\set ON_ERROR_STOP on

\getenv folios_loader_password FOLIOS_LOADER_PASSWORD
\getenv metabase_app_password METABASE_APP_PASSWORD
\getenv metabase_ro_password METABASE_RO_PASSWORD

-- Read/write role the CLI and loader connect as. Owns nothing yet —
-- `folios init` runs migrations as this role and creates
-- core/marts/staging itself.
CREATE ROLE folios_loader LOGIN PASSWORD :'folios_loader_password';
GRANT ALL PRIVILEGES ON DATABASE folios TO folios_loader;

-- Read-only role any Metabase instance's data-source connection uses
-- against the folios warehouse — whether that's your own existing
-- Metabase or the optional bundled one (metabase/compose.yml).
-- Deliberately minimal here; marts.* grants come once marts exists
-- (step 10).
CREATE ROLE metabase_ro LOGIN PASSWORD :'metabase_ro_password';
GRANT CONNECT ON DATABASE folios TO metabase_ro;

-- The optional bundled Metabase's OWN application database (dashboards,
-- questions, users, settings) — unrelated to, and fully separate from,
-- the folios warehouse. Created either way since it costs nothing if
-- unused; only populated if you run `make metabase-up`.
CREATE ROLE metabase_app LOGIN PASSWORD :'metabase_app_password';
CREATE DATABASE metabase_app OWNER metabase_app;
