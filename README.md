# Kokwet API

This directory is the deployable FastAPI backend. Render should use `backend`
as its root directory and run the command in the repository's `render.yaml`.

## Local development

From the repository root, copy `backend/.env.example` to `backend/.env`. The
example contains development-only values for local storage and required
application secrets; replace them with strong values before using any shared
or deployed environment. The backend uses SQLite by default, stored at
`backend/data/kokwet.sqlite3`.
`KOKWET_USE_SQLITE=true` explicitly keeps local runs on that SQLite database
even if a Render `DATABASE_URL` is also present in the local environment file.
Do not enable it when PostgreSQL is required; production startup rejects that
combination.

Install the backend dependencies and start the API from the `backend`
directory:

```sh
python -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
cd backend
../.venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

The local Vite frontend defaults to `http://127.0.0.1:8000/api` when opened
from `localhost` or `127.0.0.1`. Local development CORS origins remain enabled
when `KOKWET_CORS_ORIGINS` is set; `APP_ENV=production` disables those local
origins.

## Daraja sandbox callbacks

Daraja cannot call a localhost URL. For a local sandbox checkout, run the API
on port `8000`, expose it through an HTTPS tunnel (for example, `ngrok http
8000`), then set these values in `backend/.env` using credentials from the
Daraja sandbox portal:

```dotenv
MPESA_ENV=sandbox
MPESA_CALLBACK_URL=https://<your-tunnel-host>/api/v1/culture/webhook/extend-lifespan
MPESA_CONSUMER_KEY=<sandbox-consumer-key>
MPESA_CONSUMER_SECRET=<sandbox-consumer-secret>
MPESA_SHORTCODE=174379
MPESA_PASSKEY=<sandbox-passkey>
CULTURE_WEBHOOK_SECRET=<long-random-secret>
```

Use a Consumer Key and Consumer Secret from the same sandbox app with the
Lipa Na M-Pesa Sandbox product enabled. OAuth runs before STK Push, so an OAuth
HTTP 400 means no payment prompt was sent; changing the callback URL or ngrok
cannot resolve it. Check the sanitized backend error for Daraja's `error` /
`errorCode` and `error_description` / `errorMessage`, then verify the app
credentials and environment. Do not copy credentials from examples, print them,
or put them in shell commands or browser code.
Daraja access tokens are cached in process memory until 60 seconds before their
reported expiry; a backend restart clears the cache.

The culture-preservation callback uses the path in `MPESA_CALLBACK_URL`. Ticket
checkout overrides that path with `/api/v1/tickets/webhook/mpesa`. Both routes
append `CULTURE_WEBHOOK_SECRET` as a query token, so do not put the token in
`MPESA_CALLBACK_URL` yourself. Keep the tunnel and backend running until the
callback arrives. After validating and storing a callback, the endpoints return
Daraja's `{"ResultCode": 0, "ResultDesc": "Success"}` acknowledgement.
Do not use the generic `/api/mpesa/callback` path from third-party examples;
it is not a route in this backend. Use only a sandbox test phone/flow currently
specified by Daraja for your app—example phone numbers and auto-approval
behavior in copied snippets may be outdated. Never use sandbox credentials in
production or commit local `.env` values.

Ticket and culture STK requests and callbacks are implemented here. B2C
affiliate payouts are not implemented; they require separate Safaricom product
enablement, credentials, payout authorization, and callback handling. Do not
enable or attempt payouts based on the STK configuration alone.

The deployed Vercel origin `https://kokwet-lyart.vercel.app` and Kokwet Vercel
preview deployments under the `shackros-projects` team are allowed by default.
Add any other custom frontend domains to `KOKWET_CORS_ORIGINS` in Render,
separated by commas and without trailing slashes. CORS wraps the complete
FastAPI application so browser requests can also read CORS headers on server
errors instead of misreporting every 500 response as a CORS failure.

Vercel deployment protection is separate from API CORS. If a preview site's
manifest or other static assets redirect to Vercel SSO, use the production
domain or adjust that preview deployment's protection settings in Vercel.

For PostgreSQL setup, data migration, and deployment prerequisites, see
[POSTGRESQL.md](POSTGRESQL.md).
