# Render PostgreSQL deployment and SQLite migration

The backend uses SQLite for local development and tests. Production requires
`DATABASE_URL` and fails closed rather than falling back to a local database.
PostgreSQL connections use TLS and the schema configured by
`KOKWET_DB_SCHEMA`.

## Configure Render

1. Set the Render service's root directory to `backend`. The repository's
   `render.yaml` places the API in Ohio, matching the database region, and
   provides the production Uvicorn command and `/api/health` health check.
2. Set `DATABASE_URL` to the Render PostgreSQL **internal** connection URL in
   the service's Environment settings. Rotate the database password first
   because the previous external URL was shared in chat. Keep the replacement
   URL out of source control, chat, and command-line arguments.
3. Keep `APP_ENV=production`, `KOKWET_REQUIRE_DATABASE_URL=true`, and
   `KOKWET_DB_SCHEMA=kokwet_app`. Startup creates this dedicated schema if it
   does not exist, then initializes the application tables in it. The database
   role must be allowed to create schemas and tables. Application tables are
   never created in `public`.
4. Configure the ticket-encryption key, staff activation secret, M-Pesa
   credentials, CORS origins, and other integration settings listed in
   `render.yaml` before deploying. If existing encrypted ticket data is being
   migrated, its encryption key must remain available during that migration.

The backend stores its relational records and uploaded media in PostgreSQL.
Schema initialization does not copy data from the old Supabase database or a
SQLite file. Legacy compatibility endpoints read old tables in the PostgreSQL
`public` schema; migrate those records separately if they are still needed.
Legacy event passes remain historical records and are not redeemable by the
new ticket flow. The `/api/health` endpoint queries the configured database and
returns HTTP 503 when it is unavailable.

## One-time SQLite data copy

1. Back up the existing SQLite database and stop backend writes for the copy.
2. Configure the destination Render PostgreSQL URL and
   `KOKWET_DB_SCHEMA=kokwet_app` in an ignored `backend/.env` file (mode `600`)
   or in the migration shell's environment. Never commit the URL or pass it as
   a command-line argument.
3. From `backend/`, run:

   ```sh
   python migrate_sqlite_to_postgres.py --sqlite-path /path/to/kokwet.sqlite3
   ```

   Without `--sqlite-path`, the script uses `KOKWET_DB_PATH` or
   `backend/data/kokwet.sqlite3`.

The migration opens SQLite read-only, verifies that the destination application
schema has no rows, and copies every non-internal table in foreign-key order.
It preserves IDs, blobs, ticket hashes, and encrypted ticket values. Copying is
transactional, checks row counts and SHA-256 digests, and resets generated-ID
sequences. Failures roll back the destination data transaction and leave the
SQLite source untouched. Unknown or incompatible tables abort rather than
being silently skipped. Do not start production writes before the migration
finishes; the script will not merge into a populated application schema.

## Verification boundary

The SQLite end-to-end journey and PostgreSQL translation tests run locally
without a live database. A live Render PostgreSQL deployment and migration
from Supabase have not been verified here. Before switching production traffic,
test startup, the health check, critical payment/ticket/staff/live flows, and a
restored copy in a non-production Render database.
