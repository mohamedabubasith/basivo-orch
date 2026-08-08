# Basivo Agent Orchestrator

Visual agent pipelines with run logs as a first-class feature. Like n8n or
Flowise in shape, but built around the thing those tools treat as an
afterthought: **what actually happened when the pipeline ran.**

> **Beta scope.** This repository contains the landing page, the complete
> authentication layer, and the workflow engine with its Tier 1 nodes and
> dual-mode execution API. The visual canvas and the Tier 2 capability nodes
> are not here yet — see [What is not built](#what-is-not-built).

The build follows [`docs/SOW.md`](docs/SOW.md), which is the grounding
document: node taxonomy, observability requirements and the external API
contract all come from it. Phase 1 of its sequencing (§6) is done.

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

## The workflow engine

Flows are graphs. Each is versioned immutably, so a run from three weeks ago
still describes the graph that actually executed rather than today's draft.

**Tier 1 nodes** (SOW §2) — `GET /api/v1/nodes` returns the palette, including
each node's JSON Schema, so the editor cannot offer a node the engine would
reject:

| | |
| --- | --- |
| Triggers | Manual, Webhook, Scheduler |
| Utility | HTTP Request, Condition/Router, Variable/Set |

**The run log** (SOW §3) is a first-class table, not console output. One row
per node *attempt* with status, duration, input/output summaries and an error
— columns rather than a JSON blob, because "which node type fails most" has to
stay a query when there are millions of rows. A branch that was not taken is
recorded as `skipped`, never omitted: an absent row and a skipped row are
indistinguishable in aggregate, and omitting it would make every Condition
node's dead side look perfectly healthy.

### Running a flow from outside (SOW §4)

Published flows are callable with an organisation-scoped API key. Sessions
cannot serve this — a cron job or a Lambda has no cookie jar — so keys are
separate, hashed at rest, and revocable.

```bash
# blocking
curl -X POST https://api.example.com/flows/$ID/run \
     -H "Authorization: Bearer bsv_..." -d '{"input": {...}}'

# async: 202 + run_id, then poll
curl -X POST ".../flows/$ID/run?mode=async" ...   # or  Prefer: respond-async
curl ".../flows/$ID/runs/$RUN_ID"

# stream it instead
curl -N -X POST ".../flows/$ID/run/stream" ...

# or attach to a run already in progress
curl -N ".../flows/$ID/runs/$RUN_ID/stream"
```

That last one is the constraint that shaped the design. "Attach to an
in-progress run" is impossible with pub/sub alone — a client arriving at t+5s
has simply missed the first five seconds. So every event is written to
`run_event` with a gapless per-run sequence and *then* published to Redis, and
a reader subscribes **before** replaying history so nothing falls between the
two. `Last-Event-ID` resumes exactly where a dropped connection stopped.

Redis carries only the live tail. If it is down, streaming degrades to polling
and nothing is lost.

### Two guards worth knowing about

**The HTTP node cannot reach your network.** A node that fetches a
user-supplied URL is SSRF by construction; without a guard it will happily
return `169.254.169.254`'s IAM credentials as node output. Every resolved
address is checked, redirects are followed by hand and re-checked, and the
error never reveals what a hostname resolved to.

**Node config is not an expression language.** `{{ nodes.fetch.body.id }}` is a
data path — dotted keys and list indices over plain data, no calls, no
operators, no attribute access. Jinja or `eval` here would hand every author of
a flow arbitrary code execution inside the orchestrator.

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

- **162** unit tests in `apps/api/tests/` (auth, plus the engine, graph
  validation, templating and the SSRF guard)
- **67** end-to-end assertions over the live auth API: rotation and reuse
  detection, timing-equalised login, CSRF enforcement, lockout, rate limiting,
  404-not-403 for non-members
- **76** end-to-end assertions over the flow API: both run modes, SSE, attaching
  to a run mid-flight, `Last-Event-ID` resume, idempotent redelivery, tenant
  isolation and key revocation
- **40** assertions driving the auth API cross-origin exactly as the browser
  does — preflights, exposed headers, cookie flags, the full 2FA exchange

```bash
make test     # unit tests
make lint     # ruff, mypy, tsc, oxlint
make build    # production build of the web app
```

## What is not built

Named plainly, because a beta that overstates itself wastes the tester's time:

- **No visual canvas.** Flows are created and edited through the API. The graph
  format, validation and palette are all in place for one; nobody has drawn it.
- **No Tier 2 capability nodes.** Agent, Code Agent, Voice, Memory and the
  Data/Integration nodes are the product's differentiator and none exist yet.
  The node interface they will implement does, and the engine already records
  `cost_usd` / `tokens_in` / `tokens_out` for them.
- **The scheduler does not run.** `trigger.schedule` validates its cron
  expression and is otherwise inert; nothing fires it.
- **Webhook delivery is not wired.** The trigger node works when a run is
  started through the API; there is no public per-flow webhook URL yet.
- **Background runs are in-process.** `mode=async` uses an asyncio task, so a
  restart loses in-flight runs and nothing balances across workers. Correct for
  a beta, wrong for scale — the seam is one function (`execute_detached`).
- **The landing page's log stream is a scripted mock.** It is built from the
  shapes the real run viewer uses, and says so on the page.
- **Organisations, flows and runs have APIs but no UI.** All are tested; none
  have screens.

Authentication, tenancy, the run log and the execution contract came first
deliberately — they are the parts that are painful to retrofit once real users
and real integrations exist.

## A note on the auth dependency

Building this on top of `basivo-auth` surfaced five defects in it, all fixed
upstream and pulled in here: the 2FA step-up token was not consumed on use;
`install_auth` had no way to exempt an API-key-authenticated API from CSRF (so
`X-API-Key` callers were rejected with "CSRF token missing"); `init`
under-reported what an embedded host needs to type-check; and `--local`
rendered the last release instead of the working copy, which is the kind of
thing that costs an afternoon. See its CHANGELOG for 0.2.1 through 0.2.3.
