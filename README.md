# Basivo Agent Orchestrator

Visual agent pipelines with run logs as a first-class feature. Like n8n or
Flowise in shape, but built around the thing those tools treat as an
afterthought: **what actually happened when the pipeline ran.**

> **Beta scope.** This repository currently contains the landing page and the
> complete authentication layer. The pipeline builder and run engine are not
> here yet — see [What is not built](#what-is-not-built).

```
basivo-orch/
├── apps/
│   ├── api/          FastAPI. Auth embedded via basivo-auth, Postgres, Alembic
│   └── web/          React 19 + Vite + Tailwind 4. Landing page and auth UIs
├── docker-compose.yml   Postgres, Redis, Mailpit
└── Makefile
```

## Running it

Needs Docker, [uv](https://docs.astral.sh/uv/) and Node 20+.

```bash
make setup     # install deps, start containers, run migrations
make api       # terminal 1 → http://localhost:8000
make web       # terminal 2 → http://localhost:5173
```

Registration emails are caught locally by Mailpit at **http://localhost:8025** —
confirmation and password-reset links land there, not in a real inbox.

`make help` lists the rest.

## Authentication

Auth is generated and maintained by
[basivo-auth](https://github.com/mohamedabubasith/basivo-auth), installed in
*embedded* mode: it lives at `apps/api/basivo_orch/auth/` and shares this
project's SQLAlchemy `Base`, session dependency and migration history. One
engine, one connection pool, one `alembic upgrade head` — and orchestrator
tables will be able to foreign-key to `user` and `organization` directly.

What is wired up and tested end to end:

| | |
| --- | --- |
| Accounts | register, email confirmation, sign in, sign out |
| Passwords | forgot, reset, change — all revoking other sessions |
| Two-factor | TOTP enrolment with QR, step-up at login, single-use recovery codes |
| Sessions | HttpOnly cookies, refresh rotation with reuse detection |
| Tenancy | organisations with per-org roles and permission-checked routes |
| SSO | Google / GitHub / OIDC, rendered only when credentials are configured |

Auth owns `basivo_orch/auth/` and `tests/auth/` and nothing else. To pull
upstream fixes:

```bash
cd apps/api
uv sync --group tools     # installs the CLI version pinned in pyproject.toml
basivo-auth update
git diff                  # review before committing
```

The version that generated the package is recorded in
`apps/api/.copier-answers.yml`.

### Two things worth knowing before you deploy

**Cookies need a shared parent domain.** The SPA and API run on separate
origins (`localhost:5173` and `localhost:8000` in development). That works
locally because ports do not affect same-site. In production, put them on
`app.example.com` and `api.example.com` and set `COOKIE_DOMAIN=.example.com`,
or the session cookie is host-only to the API and the SPA never authenticates.

**`COOKIE_SECURE=false` is a development-only line** in `apps/api/.env`,
needed because the dev server speaks plain HTTP. The settings model refuses to
start with it false when `ENVIRONMENT` is staging or production, so it cannot
reach a deploy by accident.

### The email links are load-bearing

The API mails users at `{FRONTEND_BASE_URL}/auth/verify?token=…` and
`/auth/reset-password?token=…`. Those two frontend routes are named in
`apps/web/src/App.tsx` and must keep matching `_frontend_link()` in
`apps/api/basivo_orch/auth/email/sender.py`. Renaming one breaks every link
already sitting in someone's inbox.

This is also why the Vite dev server does **not** proxy `/auth` to the API:
those paths are pages in the SPA *and* endpoints on the API. See the comment
in `apps/web/vite.config.ts`.

## Configuration

One secret. `apps/api/.env` is generated on install with a random `SECRET_KEY`;
every other key the service needs — JWT signing, CSRF, reset and verification
tokens, OAuth state, TOTP encryption — is derived from it with HKDF at runtime.
Rotating it ends all sessions and invalidates outstanding email links.

The frontend reads one variable, `VITE_API_URL` (see `apps/web/.env.example`).
It must match the API's `PUBLIC_BASE_URL`, and the API's `CORS_ORIGINS` must
list the SPA's origin.

## Verification

The auth layer was not assumed to work — it was driven against the real stack
(Postgres, Redis, Mailpit, uvicorn):

- **110** unit tests in `apps/api/tests/auth/`
- **67** end-to-end assertions over the live API: rotation and reuse detection,
  timing-equalised login, CSRF enforcement, lockout, rate limiting, 404-not-403
  for non-members
- **40** assertions driving the API cross-origin exactly as the browser does —
  preflights, exposed headers, cookie flags, the full 2FA exchange

```bash
make test     # unit tests
make lint     # ruff, mypy, tsc, oxlint
make build    # production build of the web app
```

## What is not built

Named plainly, because a beta that overstates itself wastes the tester's time:

- **No pipeline builder.** No canvas, no nodes, no execution engine.
- **The landing page's log stream is a scripted mock.** It is built from the
  shapes the real run viewer will use, and it says so on the page.
- **No run storage.** The dashboard's counters are zeroes.
- **Organisations have an API but no UI.** Endpoints and permissions work and
  are tested; there is no screen for them yet.

Authentication, tenancy and the security posture came first deliberately —
they are the parts that are painful to retrofit once real users exist.

## Known issue

The 2FA step-up token issued at login is not consumed when used. Within its
300-second lifetime it can be exchanged at `/auth/2fa/verify` more than once,
each time minting a session. Exploiting it still requires a valid TOTP or
recovery code. Tracked upstream in basivo-auth for 0.2.1.
