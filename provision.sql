-- Deployment-side provisioning for the memory cassette.
--
-- This file is the deployment holding up its end of the manifest. The cassette
-- declares in cassette.toml what it needs; core reads that declaration,
-- publishes it, and does nothing about it. Creating the role and the database
-- is somebody else's job, and in this example that somebody is Postgres' own
-- init hook.
--
-- What changed when Cognee became the store: the cassette no longer owns a
-- schema inside the tapes database, because it no longer writes rows there.
-- Cognee keeps its own relational tables and its own vector index, so what the
-- deployment provisions is a database of its own — and, notably, the cassette
-- now takes no grant on the tapes database at all.
--
-- The names are still what the manifest derives:
--
--   role     = "cassette_" + name  ->  cassette_memory
--   database = name                ->  memory
--
-- Quoted throughout because a cassette name may legally contain a hyphen, and
-- an unquoted hyphen in SQL is a subtraction operator rather than an identifier.
--
-- Postgres runs this exactly once, when the data directory is first
-- initialized. A stale volume will skip it: `make down ARGS=-v` to reset.

CREATE ROLE "cassette_memory" LOGIN PASSWORD 'cassette';

-- Cognee's own database: its relational tables (datasets, data, pipeline runs)
-- and, with VECTOR_DB_PROVIDER=pgvector, its embeddings. Owned by the
-- cassette's role, because Cognee migrates it itself on first use the way the
-- cassette used to migrate its own schema.
CREATE DATABASE "memory" OWNER "cassette_memory";

-- pgvector has to exist inside that database, and creating an extension is a
-- superuser action — which is why it happens here rather than in the cassette.
-- The image ships the extension; this is what turns it on.
\connect "memory"
CREATE EXTENSION IF NOT EXISTS vector;
GRANT ALL ON SCHEMA public TO "cassette_memory";
