-- Grants metabase_ro read access to marts — and only marts. The role
-- itself is created by docker/initdb/01-roles.sql at container init, with
-- no grants at all yet (marts didn't exist then); this is where that gets
-- finished, now that 002_marts.sql has actually created the schema.
--
-- No grant is made on core, and none is needed: Postgres denies access to
-- a schema by default until USAGE is explicitly granted, so metabase_ro
-- already cannot see core.* — this file only ever adds to marts.
GRANT USAGE ON SCHEMA marts TO metabase_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA marts TO metabase_ro;

-- Without this, every view a later migration adds to marts is invisible
-- to Metabase until someone remembers to GRANT it by hand.
ALTER DEFAULT PRIVILEGES IN SCHEMA marts GRANT SELECT ON TABLES TO metabase_ro;
