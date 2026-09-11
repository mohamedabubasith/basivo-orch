# App Builder (beta)

A second surface next to Flows: describe a frontend in a chat, watch it appear
in a live preview beside the chat, press Deploy, send someone the link.

The beta builds **frontend applications only**. No database, no server, no
package installs. That is not a placeholder for ambition, it is the thing that
makes the first release honest: one template we have already built and tested,
one build command, one artefact to serve. Everything harder is listed at the
bottom under what beta leaves out.

## Why this belongs in basivo

The product principle is that setup is the product. A person who wants a
landing page for their flow, an internal dashboard, or a form that posts to a
webhook currently leaves basivo to build it. The coding agent that would do the
work is already here: `git.autofix` runs Claude Code headless against a working
copy today, with a throwaway HOME, no Bash, and a reviewed diff at the end.
The App Builder is that engine pointed at a project we own rather than at
somebody's repository, with a preview and a publish button attached.

## The three engines, behind one interface

`flows/nodes/search_providers.py` is the pattern: a Protocol, a registry, one
setting, and nothing about the supplier reaching the user. Coding engines get
the same treatment in `apps/api/basivo_orch/flows/nodes/engines.py`.

```python
class CodingEngine(Protocol):
    name: str                      # "claude_code" | "codex" | "opencode"
    label: str                     # what a person reads; never the model
    free: bool                     # true when the operator pays
    def available(self) -> bool: ...
    def drives(self, provider: str) -> bool: ...
    async def run(self, *, cwd: Path, prompt: str, system_prompt: str,
                  api_key: str = "", model: str = "", timeout_seconds: float = 780.0,
                  mcp_servers: McpServers | None = None,
                  allowed_mcp_tools: Sequence[str] = ()) -> EngineResult: ...
```

`EngineResult` is what the run log needs: the agent's closing message, turns,
cost when the CLI reports one, and the files it changed (computed by us with
`claude_code.snapshot` and `changed_files`, which already exist and are already
tested, rather than trusted from the agent).

| engine | binary | credential | how it is chosen |
| --- | --- | --- | --- |
| `opencode` | `opencode run` | none at all | the fallback for a workspace that has saved no credential |
| `claude_code` | `claude -p` | the workspace's own Anthropic credential | when one is saved |
| `codex` | `codex exec` | the workspace's own OpenAI credential | when one is saved |

The registry is shared, not private to the App Builder: `git.autofix` selects
from the same three (see below), so an engine added later appears on both
without a second implementation.

Three rules hold for all three, and they are the safety story:

1. **No shell.** Every engine runs with file tools only, exactly as
   `claude_code.DENIED_TOOLS` does today. The build is run by us, afterwards,
   in a process we control. An agent that can run commands on the worker is a
   remote shell for whoever is typing in the chat.
2. **The credential arrives as an environment variable in a throwaway HOME,**
   never on a command line, never in a log line, scrubbed from errors.
   `claude_code.redact` generalises to all three.
3. **The agent writes inside the project directory and nowhere else.** Codex is
   given `--sandbox workspace-write --cd <dir>`; the others have no Bash to
   escape with; and after every turn we diff against the snapshot and refuse
   changes outside the allowed paths.

### OpenCode, free for everyone

Free for all users is the reason OpenCode is there rather than as a third
option nobody picks, and the spike found it is freer than planned: the model
answers with no account and no key, so there is nothing to provision. The name
lives in `BASIVO_OPENCODE_MODEL` and is confirmed when the worker image is
built, by a real session whose packages have to land or the build fails. It
never reaches the browser, the run log, or a node's configuration: a person
sees "building", not a supplier.

Because that spend is ours, the free engine is metered: a turn counter per
organisation per day, shared by the App Builder and `git.autofix`, checked in
the same place plan limits are checked today, with a message that names the
limit and the reset time. Engines the workspace pays for itself are not
metered.

## OpenCode on the repair node, so autofix needs no key either

`git.autofix` today offers `auto`, `claude_code` and `builtin`, and every path
through it requires an LLM credential. That is the one thing standing between
a new account and a working repair bot, and OpenCode removes it.

```python
engine: Literal["auto", "opencode", "claude_code", "codex", "builtin"] = "auto"
# x-enum-labels: {"opencode": "OpenCode (free)", "codex": "Codex (OpenAI only)", ...}
```

- **The LLM credential becomes optional**, required only by the engines that
  need one. A flow whose engine is `opencode` validates and runs with a Git
  credential alone.
- **`auto` reads the credential.** No credential at all picks the free agent.
  An Anthropic credential picks Claude Code, anything else drives the built-in
  loop, exactly as before. Codex is chosen and never inherited: a flow that has
  been opening acceptable pull requests must not change engine because the
  worker image was rebuilt. `choose_engine` still returns the reason in words
  for the run log.
- **The label says free and nothing else.** Not the model name, not the
  supplier. It changes, and a flow that named it would age badly.

Same code, two callers: `git.autofix` calls the shared registry rather than
importing `claude_code` directly, which is the refactor phase 1 pays for.

## Searching the docs while it codes

A coding agent working from a training cut off writes last year's API. The fix
is the web, and the engines must not be the ones reaching it: their built in
web tools are per vendor, unlogged, and two of the three would not have one.

So search is a **tool we serve**, over stdio MCP, to all three engines:
`basivo_orch/flows/nodes/docs_mcp.py`, two tools.

- `search_the_web(query, recent=False, count=5)` calls the existing
  `search_providers.search`, so DuckDuckGo, SearXNG, and whatever replaces
  them, with news mode already built.
- `read_page(url)` is `search.readable()` behind `assert_public_url`, which is
  what keeps a link in a bug report from turning the worker into an SSRF probe.

Why MCP and not the CLIs' own web tools: one implementation, one provider
interface, one place where the run log records that documentation was on,
and `WebFetch` and `WebSearch` stay in `DENIED_TOOLS` where they belong. It is
stdio, so there is no port, no auth, and the server dies with the turn. Each
engine gets the config written into its throwaway HOME: `--mcp-config` for
Claude Code, `mcp_servers` for Codex, the `mcp` block for OpenCode.

A tick box on both nodes, "Look up documentation on the web", default on,
because the case for turning it off is speed and the case for leaving it on is
correct code. The system prompt tells the agent to search before using an API
it is unsure of, and to answer from what it read rather than from memory. That
last sentence is the one that stopped the agent inventing sports headlines, and
it works here for the same reason.

## Turns, and what a session really is

The repair node is one shot on purpose: a ticket arrives, an agent fixes it, a
pull request opens, nobody says "actually, make the button blue". The App
Builder is the opposite. The first message produces an app, and every message
after it is a correction: move that, connect this, undo the last thing. So the
question is what carries between messages, and whether that needs a machine
kept running for each person.

**The source tree is the memory.** Not an agent session, not a transcript of
tool calls: the files. A coding agent is built to walk into a directory it has
never seen and work out what is there, which is the same thing it does on turn
nine as on turn one. So what a project stores between turns is the tree, the
built output, and a short account of each turn, and none of that is tied to
which engine ran.

That is what makes the engine swappable mid-project. Every CLI here can resume
its own session (`--session` for OpenCode, `--resume` for Claude Code, `exec
resume` for Codex), and using those would look tempting and be a trap: the
session store is opaque, it is invalidated by a CLI upgrade, it can hold
prompts we would rather not keep, and it pins a project to the engine that
started it. Resuming is an optimisation we can add per engine later, under the
same contract, when a measurement says it pays.

### One turn, start to finish

1. **Take the project lock.** One turn at a time. A second message while a
   turn is running queues rather than starting a rival agent in the same tree,
   because two agents editing one directory produce a mess neither of them
   can explain.
2. **Restore the tree** into a working directory. On a worker that just ran
   this project the directory is still there and is reused, which is a cache
   and never state: a cold worker restores from the stored tar and nothing
   behaves differently.
3. **Write the context**: `AGENTS.md` (the house rules, unchanged every turn)
   and a short `CONVERSATION.md` holding the last few turns as one line each,
   plus what the previous build said. The person's new message is the prompt.
4. **Run the engine**, file tools only.
5. **Diff and gate**: writes outside `src/`, `index.html` and `public/` are
   refused, and the turn fails with what it tried to touch.
6. **Build.** On a failure the build log goes back to the agent once, and once
   only. A second failure is reported with the error, because a repair loop
   that cannot converge is how twelve minutes disappear.
7. **Store** the tree and the `dist`, append the turn, create the version, and
   let the preview reload.

### Why not a live container for each person

The obvious design is a sandbox per user, kept warm, running `vite dev` with
hot reload behind a proxy. It is what the hosted builders do, and for beta it
optimises the wrong thing. The numbers from the spike: the free agent takes
about a minute to make a small edit, `node_modules` is baked into the image so
there is no install, and a production build of a page this size is a couple of
seconds. A warm dev server would save two seconds out of sixty, and would cost
a scheduler, a reaper for idle containers, a proxy with its own authentication,
per user memory, and stickiness between a person and a machine.

So: no container per person in beta. Ephemeral working directories on stateless
workers, and the turn feels like a session because the project state is real,
not because a process is being kept alive.

### The seam, so that changing this later is not a rewrite

Everything above sits behind one small protocol, and the container version is
a second implementation rather than a new design:

```python
class Workspace(Protocol):
    async def open(self, project_id, source: bytes | None) -> Path: ...
    async def build(self, path: Path) -> BuildResult:  # dist bytes, log, ok
    async def close(self, path: Path) -> bytes: ...    # the tree, to store
```

`TempWorkspace` is the beta. `ContainerWorkspace` arrives the first time
somebody needs something a directory cannot give: installing a package the
template does not carry, running the tests, a dev server with hot reload, or
anything with a backend. That is the trigger to build it, and not before.

### Undo, which is the feature this shape gives away free

Versions are immutable and every clean build makes one, so "that made it
worse" is restoring version four as the current tree. One button, one write,
no agent involved. A person who can undo will try things, and a person who
cannot will stop asking for changes, which is the difference between a builder
they keep using and one they abandon.

## Data model

Mirrors Flows, which is already the shape people understand here.

```
app_project    id, organization_id, name, slug, engine, template,
               published_version_id, created_by, created_at, updated_at
app_version    id, project_id, version (int, per project), source_artifact_id,
               build_artifact_id, build_log, engine, turn_id, created_at
app_turn       id, project_id, run_id, prompt, reply, summary, status, created_at
```

The project carries `source_artifact_id` of its own: the tree as it stands
after the last successful turn, which is what the next turn opens.

- **Source and build output are artefacts**, one gzipped tar each, stored the
  way posters are stored today and for the same reasons: the API and the worker
  are separate containers, and Postgres is already backed up. A Vite build of
  the beta template is a few hundred kilobytes. `MAX_ARTIFACT_BYTES` is the
  ceiling, and object storage is the upgrade when it starts to hurt.
- **A version is immutable and automatic.** Every turn that builds cleanly
  makes one. Deploy does not create a version, it points
  `published_version_id` at an existing one, which is what makes rollback a
  single write.
- **No long lived containers.** A turn unpacks the source into a temp
  directory, runs the engine, builds, stores the result, and deletes the
  directory. The worker stays stateless, which is the only reason this can run
  on the workers we already have.

## Running a turn on the machinery we already have

A turn is a **run**. Each project owns one hidden single node flow (`system`
column on `flow`, excluded from the Flows list and from the API's flow
listing), whose only node is `app.build`. That buys the queue, the claim and
retry semantics, `run_event` for the live log in the chat, artefacts, per node
timeouts through `budget_seconds`, and the Runs screen for support, without a
second queue to maintain.

`app.build` does, in order:

1. Unpack the project source, or lay down the template on the first turn.
2. Write `AGENTS.md` (below) and the conversation so far.
3. Run the chosen engine with the person's message.
4. Diff the tree. Reject writes outside `src/`, `index.html`, `public/`.
5. `vite build` with the baked `node_modules`, no network, CPU and wall clock
   limits, output captured.
6. On a build failure, hand the last 100 lines of the build log back to the
   engine once. Once, not a loop: a second failure is reported to the person
   with the error, because a repair loop that cannot converge is how twelve
   minutes disappear.
7. Store source and `dist`, create the version, emit the events the chat reads.

`budget_seconds` for the node is engine timeout plus build timeout plus a
margin, and `retry_on_timeout` is False, for the reasons written in AGENTS.md.

## The template, and one AGENTS.md

One template in beta: **Vite, React 19, TypeScript, Tailwind v4**, the console's
own stack, so the thing a person builds looks like the product it came from and
we are debugging a toolchain we already run.

`node_modules` is **baked into the worker image**, exactly as the Remotion
renderer's is (`BASIVO_REMOTION_NODE_MODULES` is the precedent). No `npm
install` at build time means no network during a build, no supply chain
surface, and a build measured in seconds. It also means the dependency set is
fixed in beta, which is a real limit and is listed as one.

Every project gets the same `AGENTS.md` at its root: the house rules, the
allowed paths, the component conventions, the accessibility floor, and the
explicit instruction not to touch `package.json` or the config files. All three
CLIs read `AGENTS.md` natively, which is the whole reason to standardise on
that filename; Claude Code additionally gets it appended to its system prompt so
the rules are not merely discoverable but present.

It ships as a file in the repository (`apps/api/basivo_orch/apps/template/`)
and is versioned with the template, so a change to the rules is a diff someone
reviews.

## Preview and share links

**A different origin from the console, always.** Generated JavaScript served
from the console's origin would read the console's cookies. Previews and
published sites are served from `BASIVO_APPS_ORIGIN` (a wildcard subdomain, or
one host with a path prefix), with no credentials, a strict CSP, and
`frame-ancestors` limited to the console for drafts and open for published
versions.

```
<apps origin>/p/<project token>/            the deployed version
<apps origin>/p/<project token>/v<n>/       one version, permanent
```

The project token is the derived capability token already used for chat and
Telegram: an HMAC of the project id under `SECRET_KEY`, compared in constant
time, a uniform 404 when wrong. Unguessable, revocable by rotating, and nothing
to administer.

The preview pane is an `iframe` at the version the last turn produced. It
reloads when a turn finishes, which is what "live" means here. There is no dev
server, no HMR, no websocket, and no per user port: the delta between that and
a two second rebuild is not worth a container per session in beta.

Deploy is a button on a version. The share link is the project URL, it is shown
with a copy button, and it keeps working when the next version is deployed,
which is the point.

## Console

New section, `Apps`, above Flows.

- `/app/apps` project list: name, engine, deployed version, last activity.
- `/app/apps/:id` the builder: chat on the left (the `ChatWindow` we already
  ship, with its activity lines, markdown and attachments), preview on the
  right, a version rail under the preview with Deploy, Share and Open.
- Everything uses `components/fields.tsx`, no native controls, no
  `window.confirm`, and no dashes in copy. The existing rules apply unchanged.

## Phases

| # | What | Done when | |
| --- | --- | --- | --- |
| 0 | OpenCode headless spike: confirm the model id, the auth path and the JSON output shape | one prompt edits one file in a temp dir | done |
| 1 | `CodingEngine` interface, three implementations, `git.autofix` moved onto it, credential made optional for free engines | `pytest` covers engine choice, redaction and the tool limits | done |
| 1b | Docs MCP server, wired into all three engines, tick box on both nodes | a turn that must read current documentation gets the page, and the run log shows the search | done |
| 2 | Worker image: pinned `opencode` and `codex` CLIs beside Claude Code, a warmed OpenCode home, the template, its baked `node_modules`, `AGENTS.md` | `vite build` of the untouched template runs in the image offline, and all three CLIs answer `--version` in the image test | agents done, template to do |
| 3 | Data model, migration, `Workspace`, `app.build` node, project CRUD, the turn lock | an API level test drives three turns, the third correcting the second, and gets three versions | |
| 4 | Serving: apps origin, tokens, CSP, version and published routes | a published version loads in a browser, a wrong token gives 404 | |
| 5 | Console: list, builder, preview, version rail with Undo, Deploy and Share | a person builds, corrects, undoes and deploys without touching an editor | |
| 6 | Free tier metering, engine selection from saved credentials | the limit message names the limit and the reset | |
| 7 | basivo-qa flows for the new screens, docs, landing page mention | `/qa` covers create, turn, deploy and share | |

## What the spike settled

Four things that were guesses in this plan and are now facts, each of them
load bearing:

- **The free model needs no account at all.** `opencode run -m
  opencode/big-pickle` answered from a clean machine with no credential
  configured. Free for everyone is real, not a tier we administer.
- **Tools must be removed, not denied.** A `permission` block set to `deny`
  leaves the tool in place and waits for an approval a headless run cannot
  give, so the process hangs until its timeout. `agent.build.tools.bash =
  false` removes it, and the agent says plainly that it has no shell.
- **A cold OpenCode home is expensive twice:** a one time sqlite migration,
  and a 57MB package install it performs on its first real session. Both are
  warmed into the image now, with `config` shared by symlink and `data` copied
  per run, because sessions are a tenant's and packages are not.
- **The free model is slow**, around a minute for a trivial edit. That is
  fine for a repair and it is the reason the App Builder shows what it is
  doing rather than a spinner.

## What beta leaves out, deliberately

Backend code, databases and auth in generated apps. Installing packages the
template does not already carry. Custom domains. A dev server with HMR.
Multiple templates. Editing files by hand in the browser. Exporting to GitHub
(cheap later: `gitops` already knows how to open a pull request). Collaborative
editing. Each of these is a phase of its own, and none of them is needed to
find out whether people want the first one.

## Open questions

1. The exact OpenCode model identifier and how the operator's credential is
   provisioned, since that decides phase 0.
2. The apps origin: wildcard subdomain, or one host with a path prefix. A
   wildcard is better isolation and needs a certificate.
3. Whether free tier turns are counted per day or per project, and what the
   number is.
