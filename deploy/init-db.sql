-- Bootstrap script for the pgvector/pgvector Postgres container.
--
-- Runs once, on first-boot, via the image's /docker-entrypoint-initdb.d hook.
-- Creates the two Trellis databases and enables pgvector on the knowledge DB
-- (the operational DB doesn't need vectors).

CREATE DATABASE trellis_knowledge;
CREATE DATABASE trellis_operational;

\c trellis_knowledge
CREATE EXTENSION IF NOT EXISTS vector;
