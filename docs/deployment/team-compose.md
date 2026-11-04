# Trellis for a small team — docker compose

One machine, one Postgres, the REST API and the MCP server behind API keys. For a
handful of people who share a memory. It is not the single-user rehearsal in
[`local-compose.md`](local-compose.md) (no auth, default password) and it is not
a high-availability deployment: one host, one database, no failover.

| File | What it is |
|------|------------|
| [`docker-compose.team.yml`](docker-compose.team.yml) | The stack: `postgres` (pgvector), `api`, `mcp`. |
| [`config.team.yaml`](config.team.yaml) | Mounted as `/etc/trellis/config.yaml`. Postgres for the knowledge and operational stores, including API keys. |
| [`.env.example`](.env.example) | The settings compose reads. Copy it to `.env` (git-ignored). |

> **What has and has not been checked.** The compose file is validated with
> `docker compose config` (below); CI does not run it, so run it yourself after
> any edit. The stack has not been booted end to end by the project's CI. The
> Postgres paths of `trellis admin backup` and `trellis admin migrate` are
> exercised against a throwaway Postgres by
> `tests/unit/ops/test_postgres_ops_live.py`, which the `live-infra` workflow
> runs. Treat your first start as the smoke test, and do the restore drill below
> before you depend on a backup.

## Services

| Service | Image | Published | Notes |
|---------|-------|-----------|-------|
| `postgres` | `pgvector/pgvector:pg16` | `127.0.0.1:5432` | First boot runs [`deploy/init-db.sql`](../../deploy/init-db.sql): databases `trellis_knowledge` and `trellis_operational`, and the `vector` extension. Published on loopback only so `pg_dump` on this machine can reach it. |
| `api` | built from [`Dockerfile`](../../Dockerfile) | `TRELLIS_BIND_ADDRESS`:`8420` | `TRELLIS_AUTH_MODE=required`: every `/api/v1` request needs a key. |
| `mcp` | the same image, entrypoint `trellis-mcp` | `TRELLIS_BIND_ADDRESS`:`8421` | MCP over HTTP at `/mcp`, `TRELLIS_MCP_AUTH_MODE=required`. Reads the same key store as the API. |

There is no graph-database service. The graph and vectors live in Postgres
(`postgres` and `pgvector` backends). ArcadeDB is the blessed graph substrate for
a larger deployment; see
[`adr-arcadedb-blessed-substrate.md`](../design/adr-arcadedb-blessed-substrate.md)
before moving there.

A named volume, `trellis-data`, holds what has no Postgres backend: the SQLite
stores `outcomes.db`, `parameters.db` and `tuner_state.db`, the local blob
directory, the feedback audit log (`feedback/pack_feedback.jsonl`) and the policy
and settings files. It is shared by `api` and `mcp`.

## Set it up

Run every command from the repository root.

1. **Create `.env` and set the password.**

   ```bash
   cp docs/deployment/.env.example docs/deployment/.env
   chmod 600 docs/deployment/.env
   ```

   Set `TRELLIS_PG_PASSWORD` in it (letters and digits, or percent-encode other
   characters: it is placed in a connection URL). Or put an
   `op://<vault>/<item>/<field>` reference there and run compose under
   `op run --env-file=docs/deployment/.env --` so the value never touches disk.
   The variable is left empty in the example on purpose, and compose refuses to
   start while it is empty.

2. **Validate the file.**

   ```bash
   docker compose -f docs/deployment/docker-compose.team.yml config
   ```

   This prints the resolved stack and exits non-zero, naming the variable, if a
   required one is missing. It starts nothing. Note that it prints the resolved
   password too: do not paste its output anywhere.

3. **Start it.**

   ```bash
   docker compose -f docs/deployment/docker-compose.team.yml up -d --build
   ```

   An image built without `TRELLIS_BUILD_VERSION` reads `fallback-version` with
   no commit in `GET /api/version` and in every event's `write_provenance`. To
   stamp it, set that variable in `.env` to the version the Makefile derives from
   git: `make -n docker-build` prints it as the `--build-arg`.

4. **Mint the first keys.** With auth required, a key is needed for anything, so
   the first is minted inside the container:

   ```bash
   docker compose -f docs/deployment/docker-compose.team.yml exec api \
     trellis admin api-keys create --name operator --scopes admin --format json
   docker compose -f docs/deployment/docker-compose.team.yml exec api \
     trellis admin api-keys create --name agent --scopes read,ingest,mutate --format json
   ```

   The token prints once and only its hash is stored (in Postgres, so it survives
   recreating a container). Give each caller its own key. Scopes and the
   router-to-scope map: [`operations.md`](../agent-guide/operations.md#api-authentication).

5. **Check it.**

   ```bash
   curl -fsS http://127.0.0.1:8420/healthz
   curl -fsS http://127.0.0.1:8420/readyz
   ```

   Then point a client at `http://127.0.0.1:8421/mcp` with the key as
   `Authorization: Bearer <key>`, or call the API with `X-API-Key`
   ([`runbook.md`](runbook.md#authentication)).

6. **Look at the schema versions.** Report only; this writes nothing:

   ```bash
   docker compose -f docs/deployment/docker-compose.team.yml exec api trellis admin migrate
   ```

   Run `--apply` once to record the baseline (see "Migrating", below).

### Exposing it to other machines

Both ports bind `127.0.0.1` by default. To let teammates reach them, put a reverse
proxy that terminates TLS in front, and keep the compose ports on loopback. If you
set `TRELLIS_BIND_ADDRESS` to a non-loopback address, API keys cross that network
in clear text. Never publish the Postgres port beyond loopback.

## Backing up

A complete backup is two parts, because the data lives in two places.

**1. Postgres (documents, graph, vectors, traces, events, API keys).**
`trellis admin backup` runs `pg_dump` in custom format, one archive per database,
under `OUTPUT/postgres/`. Run it from a machine that has `trellis-ai` and a
`pg_dump` at least as new as the server (this stack runs Postgres 16). Give it a
scratch config and data directory and the connection strings through the
environment, never on a command line:

```bash
# once: a config for the backup host (config.team.yaml is the same stores)
export TRELLIS_CONFIG_DIR=/path/to/backup-config TRELLIS_DATA_DIR=/path/to/backup-data
trellis admin init
cp docs/deployment/config.team.yaml "$TRELLIS_CONFIG_DIR/config.yaml"

# each run: a file of op:// references, kept outside the repository
#   TRELLIS_KNOWLEDGE_PG_DSN=op://<vault>/<item>/knowledge-dsn
#   TRELLIS_OPERATIONAL_PG_DSN=op://<vault>/<item>/operational-dsn
op run --env-file=/path/to/backup.env -- \
  trellis admin backup --output /path/to/backups/$(date +%Y%m%d-%H%M)
```

Each DSN is `postgresql://trellis:<password>@127.0.0.1:<TRELLIS_PG_PORT>/<database>`.
The password reaches `pg_dump` through `PGPASSWORD`, never its argument list, and
is not printed, logged or written to the manifest. A missing `pg_dump` is its own
status, `tool_missing`, with exit `5`. Full behaviour:
[`operations.md`](../agent-guide/operations.md#trellis-admin-backup).

With no tools installed on the host, you can dump from inside the database
container instead. It needs no DSN and no password, because the image's default
configuration trusts connections on its local socket:

```bash
docker compose -f docs/deployment/docker-compose.team.yml exec -T postgres \
  pg_dump -U trellis -Fc trellis_knowledge > knowledge.dump
docker compose -f docs/deployment/docker-compose.team.yml exec -T postgres \
  pg_dump -U trellis -Fc trellis_operational > operational.dump
```

**2. The volume (`outcomes.db`, `parameters.db`, `tuner_state.db`, blobs, the
feedback log, policies).** Run the backup inside the `api` container and copy the
result out:

```bash
docker compose -f docs/deployment/docker-compose.team.yml exec api \
  trellis admin backup --output /var/lib/trellis/backup-1
docker compose -f docs/deployment/docker-compose.team.yml cp api:/var/lib/trellis/backup-1 ./backup-1
```

That image carries no `pg_dump`, so this run reports every Postgres store as
`tool_missing` and exits `5`: expected here, and the SQLite files and the manifest
are still written. The blob directory is only named, not copied, by the command:
copy `/var/lib/trellis/stores/blobs/` with `docker compose cp` if you store blobs.
Remove `backup-1` from the volume when you have copied it.

Keep the output off shared storage: it holds every memory, and the key hashes.

### Restoring

Stop `api` and `mcp` first
(`docker compose -f docs/deployment/docker-compose.team.yml stop api mcp`), then:

```bash
# Postgres: restore into the databases from a `trellis admin backup` output
docker compose -f docs/deployment/docker-compose.team.yml exec -T postgres \
  pg_restore -U trellis --clean --if-exists --no-owner -d trellis_knowledge \
  < OUTPUT/postgres/postgres-knowledge.dump
docker compose -f docs/deployment/docker-compose.team.yml exec -T postgres \
  pg_restore -U trellis --clean --if-exists --no-owner -d trellis_operational \
  < OUTPUT/postgres/postgres-operational.dump
```

(`pg_restore` ships in the same container; for the `pg_dump -Fc` files above use
those file names.) The volume's files go back by the steps
in [`operations.md`](../agent-guide/operations.md#restoring-a-backup). Then run the
restore drill from the same page, start the services, and confirm
`trellis admin stats --format json` shows the counts you backed up.

## Migrating

`trellis admin migrate` is the one place a store's schema version is recorded and
a schema step is applied. It is explicit: opening a store never does either, so
starting a new image never changes a database by itself.

```bash
# report: writes nothing
docker compose -f docs/deployment/docker-compose.team.yml exec api trellis admin migrate
# apply: records the baseline for each store, then any pending steps
docker compose -f docs/deployment/docker-compose.team.yml exec api trellis admin migrate --apply
```

The report names, per store, the version it is at, the version the build targets
and the steps pending. A store with no record is read from its table shape and
shown as version 1 ("from shape"). Each step and its version row commit together
or not at all. Exit `5` means a store could not be read, matched no known shape,
or a step failed (the store stays at the version it had). Details:
[`operations.md`](../agent-guide/operations.md#trellis-admin-migrate).

## Upgrading

1. Back up (above).
2. `git pull`, then rebuild and recreate:
   `docker compose -f docs/deployment/docker-compose.team.yml up -d --build`.
3. `exec api trellis admin migrate` to see what the new build would change.
   Read it; `--apply` when it is what you expect.
4. `trellis admin health --format json` and `trellis admin stats --format json`
   (in the `api` container) as the check, as in the
   [runbook](runbook.md#upgrading).

To roll back, restore the backup from step 1; a schema step is not reversed by
running an older image.

## Day-to-day

| Task | Command |
|------|---------|
| Logs | `docker compose -f docs/deployment/docker-compose.team.yml logs -f api mcp` |
| Stop, keep data | `docker compose -f docs/deployment/docker-compose.team.yml down` |
| Stop and **delete all data** | `... down -v` removes both volumes: do not run it by accident |
| Revoke a key | `exec api trellis admin api-keys revoke <KEY_ID>` |
