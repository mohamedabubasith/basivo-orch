---
name: deliver
description: Build a feature end to end in this repo the safe way. Develop at the choke point, verify with every checker, test each edge case you listed, fix until green. Use for any change that touches money, auth, quotas, or a code path with several callers.
---

# deliver

One loop, four stages. Do not skip a stage and do not reorder them.
Everything below assumes the repo root `basivo-orch`; the commands are the
ones CLAUDE.md names.

## 1. Develop

1. **Read the whole path first.** Grep every caller of the function you are
   about to change. A quota check that lives in one route and not the
   scheduler is a second bug, not a feature. Put the change where all
   callers already route through (`service.create_run`, not the four
   routes that call it).
2. **Write the edge-case table before code.** In the scratchpad, one row
   per case: input or state, expected outcome, where it is enforced. Start
   from these families and add the ones the feature owns:
   - mode and configuration: each value of the switch, the missing-config
     case, the default
   - boundaries: exactly at the limit, one over, zero, unlimited
   - time: month rollover, timezone, expiry passed by one second
   - money and state machines: every transition in and out of each state,
     the same event twice, events out of order, an event for a record we
     never issued
   - authority: each role, cross-organisation access, unauthenticated
   - failure of the outside service: timeout, 4xx, 5xx, malformed body
   - text: every sentence a user reads is one plain sentence and says what
     to do next. No em dash, no en dash, no double hyphen.
3. **Implement the smallest change that covers every row.** Reuse the
   primitives that exist (`Pill`, `Section`, `Modal`, `require()`,
   `HTTPException`). A new table gets an Alembic migration in
   `apps/api/migrations/versions` and its model imported from
   `basivo_orch/models.py`. A new root path prefix goes into the Caddyfile
   `@api` matcher and, if a third party posts to it, into
   `csrf_exempt_prefixes`. Edits inside `basivo_orch/auth/` are listed in
   `docs/generated-code-edits.md` with a guard test.
4. **A switch that disables a feature disables all of it.** When the mode
   says "off", every enforcement point returns early through one function,
   the outside service is never called, and the UI says so in one banner.

## 2. Verify

Run all of these. A failure in any one means stage 1 is not finished.

```bash
cd apps/api && uv run ruff format . && uv run ruff check . && uv run mypy basivo_orch
cd apps/api && uv run pytest -q
cd apps/web && npx tsc -b --force && npx oxlint src && npx vitest run && npm run build
```

Then start the stack (`make dev`) and open the screen you changed in a real
browser. Read it inch by inch: dark theme, empty state, error state, the
banner for the "off" mode.

## 3. Test every edge case

Turn the table from stage 1 into tests, one row each, in the order of the
table. Nothing is "obviously fine".

- API rows: `apps/api/tests/<area>/test_<feature>.py`, calling the router or
  service functions directly with a constructed `OrgContext` (see
  `tests/flows/test_credentials.py`). Webhook rows post a real signed body.
- Web rows that are logic: a `*.test.ts` beside the module.
- Web rows that are screens: a flow in `.qa/flows.yaml`, then
  `/qa http://localhost:5173 <flow ids>` and read the verdicts in
  `.qa/run/verdicts.json`.
- Text row: `uv run pytest tests/flows/test_resources.py -q` covers dashes;
  read every new sentence once more yourself.

Tick each row in the table when its test passes. A row with no test is a
row that is not done.

## 4. Fix all bugs

For every red test or QA verdict: find the root cause, fix it at the choke
point, re-run stage 2 in full. Repeat until stage 2 and stage 3 are both
green in one run. Only then commit, one commit per coherent change, author
`mohamedabu.basith <mohamedabu.basith@gmail.com>`, no trailer.

Report: what shipped, the edge-case table with its tick marks, what was
left out and why.
