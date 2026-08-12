-- Deployment-side provisioning for the memory cassette.
--
-- This file is the deployment holding up its end of the manifest. The cassette
-- declares in cassette.toml that it owns a schema and an `entries` table inside
-- it; core reads that declaration, publishes it, and does nothing about it.
-- Creating the role and letting it create its schema is somebody else's job,
-- and in this example that somebody is Postgres' own init hook.
--
-- The names are not invented here. They are what the manifest derives:
--
--   role   = "cassette_" + name  ->  cassette_memory
--   schema = name                ->  memory
--
-- Quoted throughout because a cassette name may legally contain a hyphen, and
-- an unquoted hyphen in SQL is a subtraction operator rather than an identifier.
--
-- Only CREATE on the database is granted. The cassette runs its own migration
-- at startup (store.py: DDL) and owns whatever it creates; nothing here reaches
-- into the tapes schema, because this cassette declares no views in
-- `depends.views` and so has no claim on tapes' data at all.
--
-- Postgres runs this exactly once, when the data directory is first
-- initialized. A stale volume will skip it: `make down ARGS=-v` to reset.

CREATE ROLE "cassette_memory" LOGIN PASSWORD 'cassette';

GRANT CREATE ON DATABASE tapes TO "cassette_memory";
