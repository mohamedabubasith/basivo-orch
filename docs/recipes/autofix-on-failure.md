# Recipe: auto-fix on failure

The loop this builds: **something breaks → a ticket is filed → an agent fixes
the code → a PR is waiting for review.** Nobody is paged to *start* the work;
a human only reviews the finished proposal. The error that fired at 3am is a
ticket with a linked, reviewable PR by the time anyone looks.

The key mental model: **the flow does not watch anything.** Failures are
*pushed* to it — every system that knows something broke (CI, Sentry, your
own app's error handler, GitHub itself) can call a URL, and a published flow
*is* a URL. Two of them:

- `POST /flows/{id}/run` — for callers you control (CI steps, your app).
  Authenticated with an API key.
- `/hooks/{id}` — for senders that **cannot hold an API key**, like GitHub's
  own repository webhooks. Authenticated by the webhook trigger's secret:
  GitHub's `X-Hub-Signature-256` body signature, GitLab's `X-Gitlab-Token`,
  or a plain `X-Webhook-Secret` header. Redeliveries dedupe automatically
  (the host's delivery UUID becomes the run's idempotency key).

## The tightest loop: a GitHub issue IS the trigger

No CI wiring at all — someone (or something) files an issue, GitHub calls
your flow, and a fix PR appears on the issue a few minutes later.

Flow (three nodes):

```
Webhook Trigger → Condition → Auto-fix & PR
```

1. **Webhook Trigger**: turn **Require signature** on and set a secret.
   The hook URL refuses to run without it — the secret is what stops
   strangers from spending your model budget.
2. **Condition**: `{{ input.body.action }}` equals `opened` — GitHub fires
   the same webhook for edits, closes, and label changes; only fresh issues
   should start a fix. To be pickier, add `{{ input.body.issue.title }}`
   contains `[autofix]`, so only issues opting in get the robot.
3. **Auto-fix & PR**: repo + git credential + base branch, and the problem
   assembled from the issue itself:

   ```
   Fix the problem reported in issue #{{ input.body.issue.number }}
   ({{ input.body.issue.html_url }}). Mention "#{{ input.body.issue.number }}"
   in your summary so the PR links back to it.

   Title: {{ input.body.issue.title }}

   {{ input.body.issue.body }}
   ```

   No Raise Ticket node here — the issue already exists; creating a second
   one would be noise.

Then, in the repository: **Settings → Webhooks → Add webhook**:

- **Payload URL**: the *Inbound hook* URL from the builder's Endpoints panel
  (`https://…/hooks/{flow-id}`)
- **Content type**: `application/json`
- **Secret**: the same secret you put on the trigger
- **Events**: "Let me select individual events" → **Issues** only

GitLab is the same idea: project **Settings → Webhooks**, the hook URL,
**Secret token** = the trigger's secret, trigger on **Issues events** (the
payload fields differ — read one run's input on the run detail page and
template from what you see).

That's the whole setup. From then on: issue opened → PR opened, with the
run's full step log (files read, files staged, tokens, cost) as the audit
trail, and the merge still yours.

## Build the flow (once)

1. **Credentials** (Credentials page):
   - a **GitHub** or **GitLab** credential — a personal access token with
     `repo` + `issues` scope. "Test connection" verifies it against `/user`
     before saving.
   - a **model** credential (OpenAI/Anthropic/your OpenAI-compatible host) for
     the repair agent.

2. **New flow** with four nodes:

   ```
   Webhook Trigger → Condition/Router → Raise Ticket → Auto-fix & PR
   ```

3. **Condition** (optional but recommended): only autofix what you trust it
   with. E.g. `{{ input.body.kind }}` equals `"test_failure"` → true branch
   continues; false branch can end at just the ticket.

4. **Raise Ticket**:
   - repo: `you/your-app`
   - title: `CI failure: {{ input.body.title }}`
   - body:
     ```
     {{ input.body.error }}

     Log tail:
     {{ input.body.log }}
     ```
   - Its output (`url`, `number`) is addressable by the next node.

5. **Auto-fix & PR**:
   - same repo + git credential, base branch `main`
   - problem:
     ```
     Ticket: {{ input.url }}

     {{ trigger.payload.body.error }}

     Failing log:
     {{ trigger.payload.body.log }}
     ```
   - model + model credential, and limits (`max_files`, `cost_limit_usd`) —
     the agent refuses to sprawl past them.

6. **Publish**, then open **Endpoints** in the builder header and copy the run
   URL. Create an **API key** (API keys page) for the caller.

## Point failures at it

Anything that can send HTTP is a source. The snippets below use the API-key
endpoint because these callers control their own headers; senders that don't
(GitHub, GitLab webhooks) use the `/hooks/{id}` URL as in the section above.
Three that cover most teams:

### GitHub Actions — a failing job reports itself

```yaml
  - name: Report failure to Basivo
    if: failure()
    run: |
      curl -sS -X POST "$BASIVO_FLOW_URL" \
        -H "Authorization: Bearer $BASIVO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$(jq -n \
          --arg title "CI failed: ${{ github.workflow }} on ${{ github.ref_name }}" \
          --arg error "Job ${{ github.job }} failed. Run: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" \
          --arg log "$(tail -c 4000 test-output.log 2>/dev/null || echo 'no log')" \
          '{input: {body: {kind: "test_failure", title: $title, error: $error, log: $log}}}')"
```

(`BASIVO_FLOW_URL` / `BASIVO_API_KEY` as repository secrets.)

### Your own application — the error handler reports the exception

```python
async def report_to_basivo(exc: Exception, context: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            BASIVO_FLOW_URL,
            headers={"Authorization": f"Bearer {BASIVO_API_KEY}"},
            json={"input": {"body": {
                "kind": "runtime_error",
                "title": f"{type(exc).__name__} in {context}",
                "error": "".join(traceback.format_exception(exc))[-4000:],
                "log": context,
            }}},
        )
```

### Sentry / Alertmanager / anything with outgoing webhooks

Point the integration's webhook at the flow URL with the API key header and
map its payload fields in the flow's templates — the run detail page shows
you exactly what arrived (`Run input`, verbatim), so wiring the fields is a
matter of reading one run rather than guessing.

## Why this beats "just an alert"

- An alert asks a person to start work. This delivers **finished work to
  review**: the ticket is the paper trail, the PR is the proposal, and the PR
  body is the agent's own explanation of what was wrong and what it changed.
- **A human still gates every merge.** The autofix node opens PRs; it never
  merges. If the agent cannot find the cause, the run fails loudly with the
  agent's report — no empty PRs, no silent shrugs.
- Every run is fully logged: which files the agent read, what it staged, the
  tokens and cost per step — on the run detail page, like every other run.

## Safety dials

| Config | What it bounds |
|---|---|
| `max_files` | refuses fixes that sprawl past N files |
| `max_iterations` / `max_tool_calls` | caps the agent loop |
| `cost_limit_usd` | hard spend ceiling per run |
| Condition node before autofix | only error classes you trust it with |
| branch protection on the repo | the merge stays human, enforced by the host |
