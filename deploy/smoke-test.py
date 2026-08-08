"""Smoke-test a deployment. Run it after every deploy.

    python deploy/smoke-test.py

Runs against the real thing: real TLS, real Caddy, real Postgres, production
settings and production rate limits. It exists because every deployment defect
so far — Caddy not routing bare collection paths, the bootstrap running under
dash — was invisible locally and obvious on the first real request.

The account it creates is marked verified with a direct database update over
SSH rather than by clicking an emailed link, so the test does not depend on
mail delivery being configured.
"""

import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid

import httpx


def _terraform() -> str:
    """Locate terraform.

    `shutil.which` alone is not enough: a non-interactive shell frequently has
    a narrower PATH than the one terraform was installed onto, and the failure
    is a bare FileNotFoundError that says nothing useful.
    """
    found = shutil.which("terraform")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/terraform", "/usr/local/bin/terraform"):
        if os.access(candidate, os.X_OK):
            return candidate
    sys.exit(
        "terraform is not on PATH. Either add it, or point this straight at "
        "the deployment:\n"
        "  BASIVO_URL=https://beta.basivo.in BASIVO_IP=1.2.3.4 python deploy/smoke-test.py"
    )


# Read from Terraform so this follows the deployment rather than hard-coding an
# address that goes stale the first time it is rebuilt.
def _tf(name: str) -> str:
    here = pathlib.Path(__file__).parent / "terraform"
    out = subprocess.run(
        [_terraform(), f"-chdir={here}", "output", "-raw", name],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        sys.exit(
            f"terraform output {name} failed — run this from a checkout with state.\n{out.stderr}"
        )
    return out.stdout.strip()


BASE = os.environ.get("BASIVO_URL") or _tf("site_url")
SSH = [
    "ssh",
    "-o",
    "ConnectTimeout=10",
    f"ubuntu@{os.environ.get('BASIVO_IP') or _tf('static_ip')}",
]

ok = fail = 0


def check(label, condition, extra=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}  {extra}")


def on_box(command: str) -> str:
    return subprocess.run(
        [*SSH, command], capture_output=True, text=True, timeout=90
    ).stdout.strip()


def psql(sql: str) -> str:
    return on_box(
        "cd /opt/basivo/deploy && sudo docker compose -f docker-compose.prod.yml "
        f'exec -T postgres psql -U basivo -d basivo_orch -tAc "{sql}"'
    )


def clear_limits() -> None:
    on_box(
        "cd /opt/basivo/deploy && sudo docker compose -f docker-compose.prod.yml "
        "exec -T redis sh -c \"redis-cli --scan --pattern 'LIMITS:*' | xargs -r redis-cli del\""
    )


def read_sse(response, stop=("run.succeeded", "run.failed"), limit=100):
    events, cur = [], {}
    for line in response.iter_lines():
        if line.startswith(":"):
            continue
        if line == "":
            if cur.get("event"):
                events.append((cur.get("id"), cur["event"], cur.get("data")))
                if cur["event"] in stop or len(events) >= limit:
                    break
            cur = {}
            continue
        k, _, v = line.partition(":")
        cur[k.strip()] = v.strip()
    return events


clear_limits()
email = f"live-{uuid.uuid4().hex[:8]}@basivo.in"
password = "Sc4ffold-Orchestr8-Pipelines!"
print(f"\ntarget: {BASE}\naccount: {email}\n")

c = httpx.Client(base_url=BASE, timeout=90)

print("1. account")
H = {"X-CSRF-Token": c.get("/auth/csrf").headers["x-csrf-token"]}
r = c.post("/auth/register", json={"email": email, "password": password}, headers=H)
check("register -> 201", r.status_code == 201, f"{r.status_code} {r.text[:140]}")

# Sign-in does not require a verified address in this build — fastapi-users'
# `requires_verification` is off, so verification gates features rather than
# access. Recorded rather than asserted either way: it is a product decision,
# and worth knowing that email is not currently a hard gate on sign-in.
r = c.post("/auth/login", data={"username": email, "password": password})
print(
    f"  NOTE  unverified sign-in returns {r.status_code} "
    f"({'allowed' if r.status_code == 200 else 'blocked'}) — product decision, see below"
)

# Stands in for clicking the emailed link.
psql(f"UPDATE \\\"user\\\" SET is_verified = true WHERE email = '{email}'")
check(
    "account marked verified",
    psql(f"SELECT is_verified FROM \\\"user\\\" WHERE email='{email}'") == "t",
)

r = c.post("/auth/login", data={"username": email, "password": password})
check("login -> 200", r.status_code == 200, f"{r.status_code} {r.text[:140]}")
H = {"X-CSRF-Token": r.headers["x-csrf-token"]}

cookies = r.headers.get_list("set-cookie")
session_cookie = next((s for s in cookies if "session=" in s), "")
check(
    "session cookie is Secure in production",
    "secure" in session_cookie.lower(),
    session_cookie[:120],
)
check("session cookie is HttpOnly", "httponly" in session_cookie.lower())

r = c.get("/users/me")
check("GET /users/me -> 200", r.status_code == 200, r.status_code)
check("it is our account", r.status_code == 200 and r.json()["email"] == email)

print("\n2. the proxy is not lying about client addresses")
# TRUSTED_PROXY_COUNT=1. If it were 0 the API would read Caddy's container
# address for every request on earth, and lockout, rate limiting and the audit
# trail would all key on one "user".
ip = psql(
    f"SELECT ip_address FROM audit_event WHERE user_id = "
    f"(SELECT id FROM \\\"user\\\" WHERE email='{email}') ORDER BY created_at DESC LIMIT 1"
)
check(
    f"audit recorded a real client IP ({ip})",
    ip not in ("", "172.18.0.1", "127.0.0.1", None),
    ip,
)

print("\n3. workspace and flow")
r = c.post(
    "/orgs", json={"name": "Live", "slug": f"live-{uuid.uuid4().hex[:8]}"}, headers=H
)
check(
    "create organisation -> 201",
    r.status_code == 201,
    f"{r.status_code} {r.text[:140]}",
)
org = r.json()["id"]

GRAPH = {
    "nodes": [
        {"id": "t", "type": "trigger.manual", "name": "Start", "config": {}},
        {
            "id": "fetch",
            "type": "http.request",
            "name": "Fetch",
            "config": {"url": "https://httpbin.org/delay/1", "method": "GET"},
        },
        {
            "id": "check",
            "type": "logic.condition",
            "name": "OK?",
            "config": {
                "comparisons": [
                    {
                        "left": "{{ nodes.fetch.status }}",
                        "operator": "equals",
                        "right": 200,
                    }
                ]
            },
        },
        {
            "id": "good",
            "type": "data.set",
            "name": "Success",
            "config": {
                "assignments": [
                    {"name": "outcome", "value": "ok"},
                    {"name": "who", "value": "{{ trigger.payload.name }}"},
                ],
                "replace_output": True,
            },
        },
        {
            "id": "bad",
            "type": "data.set",
            "name": "Failure",
            "config": {
                "assignments": [{"name": "outcome", "value": "failed"}],
                "replace_output": True,
            },
        },
    ],
    "edges": [
        {"source": "t", "target": "fetch"},
        {"source": "fetch", "target": "check"},
        {"source": "check", "target": "good", "source_handle": "true"},
        {"source": "check", "target": "bad", "source_handle": "false"},
    ],
}
r = c.post(
    f"/api/v1/orgs/{org}/flows", json={"name": "Live triage", "graph": GRAPH}, headers=H
)
check("create flow -> 201", r.status_code == 201, f"{r.status_code} {r.text[:140]}")
flow = r.json()["id"]

r = c.post(f"/api/v1/orgs/{org}/flows/{flow}/publish", headers=H)
check("publish -> 200", r.status_code == 200, f"{r.status_code} {r.text[:140]}")

r = c.post(f"/api/v1/orgs/{org}/api-keys", json={"name": "live"}, headers=H)
check("create API key -> 201", r.status_code == 201, r.text[:140])
key = r.json()["key"]
api = httpx.Client(
    base_url=BASE, timeout=120, headers={"Authorization": f"Bearer {key}"}
)

print("\n4. mode 1 — blocking, over the internet")
t0 = time.perf_counter()
r = api.post(f"/flows/{flow}/run", json={"input": {"name": "Ada"}})
elapsed = time.perf_counter() - t0
check(
    "POST /flows/{id}/run -> 200",
    r.status_code == 200,
    f"{r.status_code} {r.text[:200]}",
)
body = r.json() if r.status_code == 200 else {}
check("run succeeded", body.get("status") == "succeeded", body.get("error"))
check(f"it really waited ({elapsed:.1f}s)", elapsed > 1.0, f"{elapsed:.2f}s")
check(
    "the taken branch is the result",
    body.get("output", {}).get("result", {}).get("who") == "Ada",
    body.get("output"),
)
sync_run = body.get("id")

print("\n5. the run log")
r = api.get(f"/flows/{flow}/runs/{sync_run}")
by_id = {n["node_id"]: n for n in r.json()["nodes"]}
check(
    "every node recorded",
    set(by_id) == {"t", "fetch", "check", "good", "bad"},
    sorted(by_id),
)
check("untaken branch is skipped, not missing", by_id["bad"]["status"] == "skipped")
check(
    "outbound HTTP worked from the instance",
    by_id["fetch"]["status"] == "succeeded",
    by_id["fetch"].get("error"),
)
check(
    "per-node durations recorded",
    all(n["duration_ms"] is not None for n in by_id.values()),
)

print("\n6. mode 2 — async + poll")
r = api.post(f"/flows/{flow}/run?mode=async", json={"input": {"name": "Grace"}})
check("?mode=async -> 202", r.status_code == 202, f"{r.status_code} {r.text[:140]}")
async_run = r.json()["run_id"]
final = None
for _ in range(60):
    poll = api.get(f"/flows/{flow}/runs/{async_run}").json()
    if poll["status"] in ("succeeded", "failed"):
        final = poll
        break
    time.sleep(1)
check("polling reached a terminal state", final is not None)
check(
    "async run succeeded",
    final and final["status"] == "succeeded",
    final and final.get("error"),
)

print("\n7. SSE through Caddy — the part a reverse proxy usually breaks")
first_at = None
with api.stream(
    "POST", f"/flows/{flow}/run/stream", json={"input": {"name": "Alan"}}
) as response:
    check("stream -> 200", response.status_code == 200, response.status_code)
    check(
        "content-type is text/event-stream",
        "text/event-stream" in response.headers.get("content-type", ""),
        response.headers.get("content-type"),
    )
    started = time.perf_counter()
    events, cur = [], {}
    for line in response.iter_lines():
        if first_at is None and line.startswith("event:"):
            first_at = time.perf_counter() - started
        if line.startswith(":"):
            continue
        if line == "":
            if cur.get("event"):
                events.append((cur.get("id"), cur["event"], cur.get("data")))
                if cur["event"] in ("run.succeeded", "run.failed"):
                    break
            cur = {}
            continue
        k, _, v = line.partition(":")
        cur[k.strip()] = v.strip()
    total = time.perf_counter() - started

kinds = [e[1] for e in events]
check("begins with run.started", kinds and kinds[0] == "run.started", kinds[:3])
check("ends with run.succeeded", kinds and kinds[-1] == "run.succeeded", kinds[-3:])
check(
    "event ids are the gapless sequence",
    [int(e[0]) for e in events] == list(range(1, len(events) + 1)),
    [e[0] for e in events][:10],
)
# The flow's first node sleeps a second. If Caddy were buffering, nothing would
# arrive until the whole run finished and these two numbers would be equal.
check(
    f"events arrive progressively, not all at the end "
    f"(first at {first_at:.2f}s, run took {total:.2f}s)",
    first_at is not None and first_at < total * 0.6,
    f"first={first_at} total={total}",
)

print("\n8. cross-mode attach")
r = api.post(f"/flows/{flow}/run?mode=async", json={"input": {"name": "Cross"}})
attach_run = r.json()["run_id"]
time.sleep(0.5)
mid = api.get(f"/flows/{flow}/runs/{attach_run}").json()["status"]
with api.stream("GET", f"/flows/{flow}/runs/{attach_run}/stream") as response:
    attached = read_sse(response)
check(
    f"attached while still running (status was {mid})",
    mid in ("queued", "running"),
    mid,
)
check("replays what was missed", attached and attached[0][1] == "run.started")
check("follows through to the end", attached and attached[-1][1] == "run.succeeded")
check(
    "no gap between replay and live tail",
    [int(e[0]) for e in attached] == list(range(1, len(attached) + 1)),
)

print("\n9. isolation and revocation")
r = httpx.post(f"{BASE}/flows/{flow}/run", json={}, timeout=30)
check("no API key -> 401", r.status_code == 401, r.status_code)
r = httpx.post(
    f"{BASE}/flows/{flow}/run", json={}, headers={"X-API-Key": key}, timeout=90
)
check(
    "X-API-Key works (CSRF exemption is wired)",
    r.status_code == 200,
    f"{r.status_code} {r.text[:100]}",
)

key_id = c.get(f"/api/v1/orgs/{org}/api-keys", headers=H).json()[0]["id"]
c.delete(f"/api/v1/orgs/{org}/api-keys/{key_id}", headers=H)
r = api.post(f"/flows/{flow}/run", json={})
check("a revoked key stops working immediately", r.status_code == 401, r.status_code)

print("\n10. rate limiting is live in production")
clear_limits()
codes = [
    httpx.post(
        f"{BASE}/auth/login",
        data={"username": f"x-{uuid.uuid4().hex[:6]}@basivo.in", "password": "nope"},
        timeout=30,
    ).status_code
    for _ in range(8)
]
check("login burst is throttled", 429 in codes, codes)
clear_limits()

print(f"\n{'=' * 56}\n  {ok} passed, {fail} failed\n{'=' * 56}")
sys.exit(1 if fail else 0)
