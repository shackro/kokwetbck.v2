# Kokwet API

This directory is the deployable FastAPI backend. Render should use `backend`
as its root directory and run the command in the repository's `render.yaml`.

The deployed Vercel origin `https://kokwet-lyart.vercel.app` is allowed by
default. Add any custom frontend domains to `KOKWET_CORS_ORIGINS` in Render,
separated by commas and without trailing slashes. CORS wraps the complete
FastAPI application so browser requests can also read CORS headers on server
errors instead of misreporting every 500 response as a CORS failure.

For PostgreSQL setup, data migration, and deployment prerequisites, see
[POSTGRESQL.md](POSTGRESQL.md).
