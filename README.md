# Kokwet API

This directory is the deployable FastAPI backend. Render should use `backend`
as its root directory and run the command in the repository's `render.yaml`.

The deployed Vercel origin `https://kokwet-lyart.vercel.app` is allowed by
default. Add any custom frontend domains to `KOKWET_CORS_ORIGINS` in Render,
separated by commas and without trailing slashes.

For PostgreSQL setup, data migration, and deployment prerequisites, see
[POSTGRESQL.md](POSTGRESQL.md).
