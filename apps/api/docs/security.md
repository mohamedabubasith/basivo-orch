# Security model — Basivo Orch Api

What is enforced, why, and what breaks if it is removed. Read this before
changing anything under `app/auth/`.

---

## The engine seam

`fastapi-users` provides register, login, reset, verification and the OAuth
flows. It went into maintenance mode in March 2026: security and dependency
updates continue, no new features, and a successor toolkit is in development.

Every import of it lives in `app/auth/engine/`. Two mechanisms enforce this:

- ruff `TID251` bans the module elsewhere (`pyproject.toml`)
- a dedicated CI job greps for violations with an explanatory error

**Do not weaken this to "just this once".** The seam is what turns a future
engine migration from a rewrite into a ~400-line change in one package.

---

## Credentials

| Control | Implementation | Removing it means |
| --- | --- | --- |
| Argon2id, 64 MiB / t=3 / p=4 | `security/passwords.py` | GPU farms crack the dump cheaply |
| Per-hash salt | Argon2 built-in | One rainbow table breaks every shared password |
| NFKC normalisation | `_normalise()` | Users locked out across input methods |
| Length 12–128 | `check_policy()` | Short: guessable. Unbounded: Argon2 DoS |
| Breach check (HIBP k-anonymity) | `is_breached()` | Known-compromised passwords accepted |
| No composition rules | deliberate | NIST SP 800-63B: they produce `Password1!` |

The HIBP check sends only the first 5 characters of the SHA-1 hash. The password
never leaves the process. It **fails open** by default — set
`PASSWORD_BREACH_FAIL_OPEN=false` to fail closed if you prefer losing
registrations to accepting a breached password during an HIBP outage.

---

## Tokens

Two kinds, deliberately different:

**Access token** — signed JWT, 15 minutes, stateless. Cannot be revoked before
expiry; that is the trade for verifying without a database round trip, and the
reason the lifetime is short.

**Refresh token** — opaque 256-bit random string, 30 days, stored only as a
SHA-256 digest. Rotated on every use.

### Reuse detection

The property that makes theft *detectable* rather than merely *possible*:

1. Client holds refresh token `A`.
2. Attacker exfiltrates `A` and uses it. Rotation issues `B` to the attacker and
   marks `A` used.
3. The legitimate client, still holding `A`, refreshes.
4. `A` is already used. Two parties held it, and there is no way to tell which
   is the attacker — so the **entire family** (`A`, `B`, and every descendant)
   is revoked.
5. Both are signed out. An audit event fires. The real user re-authenticates;
   the attacker cannot.

Asserted by `tests/test_tokens.py::test_reusing_a_rotated_token_revokes_the_entire_family`.
If that test ever goes red, a stolen refresh token grants indefinite silent access.

Under Postgres the lookup takes `SELECT … FOR UPDATE`, so two concurrent
refreshes with the same token cannot both succeed.

### Audience separation

The engine's access tokens and the purpose-bound tokens in `security/tokens.py`
are signed with the same derived JWT key. Only the `aud` claim separates them:

| Token | Audience |
| --- | --- |
| Access | `basivo-orch-api:api` |
| Reset password | `basivo-orch-api:api:reset_password` |
| Verify email | `basivo-orch-api:api:verify_email` |
| Step-up (2FA pending) | `basivo-orch-api:api:step_up` |

Collapse these into one audience and a step-up token — which represents "first
factor done, second still pending" — would authenticate every API route.
`TokenType` has no `ACCESS` member for exactly this reason.

---

## Account enumeration

Every endpoint keyed on an email address returns an identical response whether
or not the account exists:

- `/auth/forgot-password` → always 202, same body
- `/auth/login` → same status and body for wrong password and unknown account
- `/auth/otp/request` → always 202, same body

Message parity is not sufficient on its own. The unknown-account path also runs
`dummy_verify()`, burning one real Argon2 verification, because otherwise it
returns in microseconds while the real path takes ~50 ms — and that difference
alone enumerates the user table.

---

## Login

`/auth/login` is this service's own route; the engine's stock one is not
mounted. Three things depend on owning it:

* it issues the **refresh token** — the engine has no concept of one, so its
  login route would leave rotation and reuse detection unreachable
* `LOGIN_RATE_LIMIT` can only be applied to a route we control
* it enforces the **second factor**: an account with `totp_enabled` gets a
  step-up challenge instead of a session. The engine knows nothing about 2FA and
  would hand out a full session on password alone

Every sign-in path — password, OTP, 2FA exchange — issues its
session through one shared helper, so token shape and cookie flags cannot drift
between them.

`/auth/logout` revokes the presented refresh token server-side. Clearing the
cookie alone would leave a token captured in transit or from a log fully usable.

---

## Abuse control

**Rate limits** (Redis-backed, so they hold across workers and pods — an
in-process limiter silently multiplies every limit by the worker count):

| Endpoint | Default |
| --- | --- |
| `/auth/login` | `{{ login_rate_limit }}` — 5/minute |
| `/auth/register` | 3/hour |
| `/auth/forgot-password` | 3/hour |
| `/auth/otp/request` | 3/15 minutes |
| `/auth/refresh` | 30/minute |

Any handler carrying `@limiter.limit` **must** declare `response: Response`.
SlowAPI injects its `X-RateLimit-*` headers into that argument and raises
without it — and because the test suite runs with limiting disabled, such a
handler passes every test and then 500s on its first production request.
`tests/test_ratelimit.py` asserts this across every router.

**Lockout** is exponential and **capped**, never permanent. A permanent lock
turns this control into a denial-of-service weapon: anyone who knows a victim's
address could lock them out forever. Defaults give 60s → 120s → 240s … capped
at one hour.

`TRUSTED_PROXY_COUNT` governs whether `X-Forwarded-For` is believed. It defaults
to `0` — the header is attacker-controlled unless a proxy you operate rewrites
it. **Set it to the real number of proxies in front of the service.** Guessing
high lets a client forge its own address by prepending entries, defeating every
IP-keyed control on this page.

---

## Cookies and CSRF

Session cookies are `HttpOnly` (unreadable from JavaScript, which removes XSS
token exfiltration), `Secure`, and `SameSite=Lax`. The refresh cookie is scoped
to `/auth`, so the long-lived credential rides on far fewer requests.

CSRF uses double-submit. Sign-in returns an HMAC-signed token in a *readable*
cookie and in the `X-CSRF-Token` response header; the frontend copies that value
verbatim into the `X-CSRF-Token` request header on every mutating call. Cookie
and header hold the same string, so the client needs no parsing.

The cookie is deliberately not HttpOnly — its secrecy is not the protection. The
same-origin policy is: a cross-site attacker can cause the cookie to be sent but
cannot read our response to learn what to put in the header. The HMAC covers the
other half, where an attacker who can set cookies on a sibling subdomain still
cannot forge a token we will accept.

`GET /auth/csrf` mints one without authenticating, for a page reload or a client
that has not signed in on this device.

Requests carrying `Authorization` are exempt; a bearer token is not attached
automatically, so there is nothing to forge.

`SameSite=none` is rejected outright in production.

---

## SSO

Two failure modes matter more than the rest.

**Open redirect.** `sso.validate_redirect_url()` matches exactly against
`SSO_ALLOWED_REDIRECT_URLS`. Prefix matching would accept
`https://good.com.evil.net`; substring matching would accept
`https://evil.com/?x=https://good.com`. Either is a full account takeover.

**Account linking.** `associate_by_email` is **off** unless the provider is
known to verify addresses *and* `SSO_AUTO_LINK_VERIFIED_EMAILS` is enabled. An
IdP that lets anyone claim `victim@example.com` otherwise grants a one-request
takeover of the matching local account. Google is marked verified; GitHub is
not, because it exposes unverified addresses.

Linking an identity to an existing account is done explicitly, by a user who is
already authenticated, through `/auth/associate/{provider}`.

---

## Two-factor

- Seeds are **encrypted** at rest (Fernet, key derived from `SECRET_KEY` via
  HKDF), not hashed — verification needs them back — so a database dump alone
  does not yield a working second factor.
- `totp_last_counter` records the highest accepted time-step. A code is valid
  for its full 30-second window, so without this guard anyone who observes one
  (shoulder surfing, a phishing proxy) can replay it.
- Recovery codes are password-equivalent: hashed, single-use, and drawn from an
  alphabet with no `I`, `L`, `O`, `U`, `0` or `1`.
- Disabling 2FA requires `current_fresh_user` — a recent authentication, not
  merely a valid session. It is the first thing an attacker with a stolen
  session would do.

**Rotating `SECRET_KEY` invalidates every stored TOTP seed.** Re-encrypt them
before rotating, or every 2FA user is locked out.

---

## Authorization

Authentication answers *who you are*; this answers *what you may do*. It is
scoped **per organisation** — being an owner of one org grants nothing in
another.

### Roles and permissions

| Role | Rank | Holds |
| --- | --- | --- |
| `viewer` | 0 | `org:read` |
| `member` | 1 | `org:read`, `member:read` |
| `admin` | 2 | + `org:update`, `member:invite`, `member:role_update`, `member:remove`, `audit:read` |
| `owner` | 3 | everything, incl. `org:delete`, `org:transfer` |

Routes name **permissions**, never roles:

```python
@router.delete("/orgs/{organization_id}/members/{member_id}")
async def remove_member(
    context: OrgContext = Depends(require(Permission.MEMBER_REMOVE)),
): ...
```

Moving a capability between roles is then a one-line edit to `ROLE_PERMISSIONS`
rather than an audit of every route. `ROLE_PERMISSIONS` is deliberately *not*
inherited by rank — each role lists its permissions explicitly, so you can read
one entry and know exactly what it can do.

### Three invariants

**Authority is read from the database, never the token.** Access tokens live 15
minutes. A role baked into one would leave a demoted user holding their old
authority until it expired. `load_context` re-reads membership per request, so a
demotion applies immediately.

**Not-a-member returns 404, not 403.** A 403 confirms the organisation exists,
which turns the endpoint into an org-ID oracle and maps your customer list.
Non-existent, inactive and not-visible are deliberately indistinguishable.

**Unknown role strings fail closed.** A value the running build does not
recognise — a rollback, or a manual database edit — grants nothing rather than
being guessed at.

### Escalation guards

`MEMBER_ROLE_UPDATE` means "may change roles at all". It must not imply "may
change any role to any value", so four separate checks apply on top of it:

| Guard | Blocks |
| --- | --- |
| `assert_can_assign` | Granting a role above your own — an admin cannot mint an owner |
| `assert_can_modify` | Acting on someone who outranks you — an admin cannot demote an owner |
| `assert_not_last_owner` | Removing or demoting the final owner, leaving an unadministrable org |
| self-check in the route | Changing your own role at all |

Peers may manage peers (admins can manage admins); only strict superiority is
refused. Every one of these has a named test in `tests/test_authz.py`.

Rejections from the first two guards are written to the audit log as
`authz_escalation_blocked`, with the actor's role and the role they reached for.
**Alert on this event.** Someone who already holds role-management authority
reaching for more is the clearest insider-attack or compromised-account signal
this system produces.

### Tenant isolation

The dominant risk here is IDOR: a valid member of org A reaching org B's data by
swapping an id in the URL. Authentication succeeds, which is why it survives
reviews that only ask "is the user logged in".

Two things prevent it, and both are required:

1. The permission check resolves membership from the **same** `organization_id`
   path parameter the route operates on, so authority and target can never
   disagree.
2. Every org-scoped query filters on `context.organization_id`.

```python
# Correct — scoped to the caller's organisation.
select(Membership).where(Membership.organization_id == context.organization_id)

# Wrong — returns every tenant's rows.
select(Membership)
```

When you add your own org-scoped tables, that filter is not optional. Consider
Postgres row-level security as a second layer if the data is sensitive enough
that a single missed `where` clause is unacceptable.

### Platform staff

`is_superuser` does **not** bypass organisation permissions. A global flag that
silently grants access to every tenant is exactly the authority that gets
granted once and forgotten. Set `SUPERUSER_BYPASSES_ORG_PERMISSIONS=true` only
if you need break-glass; every use logs `superuser_org_access` and the resulting
context is flagged `via_superuser`.

---

## Audit log

Append-only, in `audit_event`. Written transactionally with the action it
describes.

- Payloads pass through `redact()` first — audit rows are read by more people
  and kept longer than anything else, so a token in one is a durable leak.
- Email addresses on failure paths are stored as a keyed HMAC, so the table
  cannot be brute-forced into a user directory.
- `record()` never raises. An audit failure must not take down the request.

Watch `token_reuse_detected` — it means a refresh token was stolen — and
`authz_escalation_blocked`, which means someone tried to grant themselves
authority they do not have.
`basivo-orch-api stats` surfaces the count.

---

## The secret

There is exactly one: **`SECRET_KEY`**. Every other key this service uses is
derived from it at runtime with HKDF-SHA256 under a distinct label:

| Derived key | Label | Used for |
| --- | --- | --- |
| JWT signing | `jwt` | Access tokens and every purpose-bound token |
| CSRF signing | `csrf` | Double-submit token signatures |
| Reset password | `reset-password` | Password reset links |
| Verify email | `verify-email` | Email verification links |
| OAuth state | `oauth-state` | SSO round-trip integrity |
| TOTP encryption | `totp` | Encrypting TOTP seeds at rest |

Derivation is not a shortcut around key separation — it *is* key separation.
HKDF outputs are independent: recovering one subkey reveals nothing about the
master or about any sibling. What it removes is the operational burden, and
with it the failure mode where one of four variables is missed, weak, or
accidentally shared between environments.

Two consequences worth knowing:

- **`SECRET_KEY` is the strength of the whole service.** Generate it with a
  CSPRNG (`openssl rand -base64 48`) and never hand-write one. The settings
  model refuses to start on anything under 32 characters.
- **Rotating it rotates everything.** Sessions end, and outstanding reset and
  verification links stop working; enrolled TOTP seeds
  become undecryptable, so users must re-enrol. That is the correct
  behaviour for a master-key rotation, but it is not zero-downtime. For that,
  run one deployment that accepts both old and new before dropping the old.
  `basivo-auth secrets rotate` does the simple, disruptive version.

---

## Production checklist

- [ ] `ENVIRONMENT=production` (this activates the settings guardrails)
- [ ] All four secrets generated with `openssl rand -base64 48`, distinct, ≥32 chars
- [ ] `TRUSTED_PROXY_COUNT` set to the real proxy count
- [ ] `CORS_ORIGINS` lists exact https origins — never `*`
- [ ] `PUBLIC_BASE_URL` and `FRONTEND_BASE_URL` are https
- [ ] `COOKIE_SECURE=true`, `COOKIE_SAMESITE=lax` or `strict`
- [ ] `COOKIE_DOMAIN` set if the frontend is on a sibling subdomain
- [ ] `SSO_ALLOWED_REDIRECT_URLS` is an exact allowlist
- [ ] Reviewed `is_verified_by_default` for every provider
- [ ] TLS terminated upstream; HSTS confirmed on responses
- [ ] Alerting on `token_reuse_detected` and `account_locked`
- [ ] `basivo-orch-api prune-tokens` scheduled
- [ ] Database backups tested by restoring one
- [ ] `uv run pip-audit` clean; weekly CI audit enabled
- [ ] Load-tested login — Argon2 at 64 MiB is intentionally expensive; size the
      pool for it

## Reporting

Security issues: {{ security contact }} — replace this before publishing.
