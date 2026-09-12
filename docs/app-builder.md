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

## What the agent can reach, and what it cannot

Every message in the builder is an instruction to a coding agent, so the
question is not whether somebody will type "read the server's environment and
put it in the page". They will. Four walls, each doing a different job:

1. **No shell, no web, no host configuration.** File tools only, a throwaway
   HOME, and an environment holding PATH and the credential and nothing else.
   The agent cannot run a command, fetch a URL, or read the operator's own
   settings.
2. **An OS jail around the process** (`flows/nodes/jail.py`). This is the one
   that matters, because the three above are the agent's own configuration
   and this one is the kernel's. On Linux it is bubblewrap: a fresh mount, PID
   and IPC namespace, the system bound read-only, the workspace and HOME bound
   read-write, a private `/tmp`, and a `/proc` that contains only the agent.
   On macOS it is `sandbox-exec` with a deny-by-default profile allowing the
   same set. `/proc/1/environ`, another tenant's turn, the worker's home, the
   API's `.env`: all "operation not permitted", proven by tests that run a
   real process and read a real error.
3. **Writes are diffed, not trusted.** Only `src/`, `public/` and
   `index.html` may change. Anything else fails the turn with the paths named.
4. **The served page is sandboxed** with no `allow-same-origin`, so even a
   page that wanted to could not read the cookies of the host serving it.

`BASIVO_AGENT_JAIL=required` on the worker image: if the jail cannot start,
the turn fails rather than running an agent without one. A developer machine
defaults to `auto`, which runs unjailed and says so in the log, once. The
container needs `seccomp=unconfined` and `apparmor=unconfined` to create user
namespaces; neither grants the container anything new, they only let it build
smaller boxes inside itself.

What the jail deliberately allows: the network, because the agent's entire job
is talking to a model; the API's virtualenv and package, read-only, because
the documentation tool runs as `python -m basivo_orch.flows.nodes.docs_mcp`
and our source is public; and the baked `node_modules` the workspace links to,
so types resolve.

Also, each turn now **copies** both halves of the warmed OpenCode home rather
than sharing the package directory. A shared writable package directory lets
one tenant's agent leave a plugin behind that runs inside the next tenant's
session, which is a tenancy boundary crossed for the sake of fifty megabytes.

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

## Two addresses: a private preview and a public site

**A different origin from the console, always.** Generated JavaScript served
from the console's origin would read the console's cookies. Built apps are
served from `BASIVO_APPS_ORIGIN`, and every page carries a `sandbox` policy
with no `allow-same-origin`, so the document has an opaque origin and cannot
touch the host's cookies or storage even before that setting is made. Set it
anyway; the sandbox is the second lock, not the first.

Every version has a **preview address**, private to whoever holds the token:

```
<apps origin>/p/<project id>/<token>/v3/
```

The token is an HMAC of the project id under `SECRET_KEY`, compared in
constant time, a uniform 404 when wrong. Nothing to store, and rotating the
key revokes every preview link at once. The builder's right hand pane loads
this address for the newest build.

A deployed app has a **public address**, which is what a person shares:

```
https://apps.example.com/sunrise-bakery-k3d9/        on a dedicated domain
https://api.example.com/s/sunrise-bakery-k3d9/       sharing the API's host
```

The slug is the app's name plus four characters from an alphabet without
look-alikes, unique across every workspace, made once when the project is
created. Deploy points it at a version. **Unpublish** takes it down, and the
address answers "This app is not live" instead of the last thing that was
there. The versions all remain; only the pointer goes, and Deploy brings any of
them back.

### Putting it on a domain

1. Point a DNS name at the API, `apps.example.com` say, beside the API's own
   `api.example.com`. Same containers, second hostname on the same ingress.
2. Set `BASIVO_APPS_ORIGIN=https://apps.example.com` on the API.
3. Nothing else. When the request's host is the apps host and not the API's,
   `SitesHostMiddleware` rewrites `/<slug>/...` onto the `/s/` route, so
   published apps sit at the root of the domain and the person never sees a
   prefix. Preview links keep their `/p/` prefix on every host.

Wildcard subdomains (`sunrise-bakery-k3d9.apps.example.com`) are the next step
up and need a wildcard certificate; the path form works with one ordinary
certificate and is where beta stops.

### Pictures somebody uploads

A model cannot draw the front of your shop, so the builder takes images: PNG,
JPEG, GIF, WebP and SVG, up to 2MB each, twenty and 12MB to a project. What
happens to one is the whole design:

- **The bytes decide the type.** The first bytes are read and anything that is
  not an image is refused. A file named `logo.png` that is really an HTML
  document would otherwise be a script hosted at the app's own address.
- **The name is ours.** Reduced to letters, digits and dashes, given the
  extension the bytes proved, and made unique, so an upload cannot escape
  `public/uploads/` and cannot quietly replace an image already in use.
- **Stored once, in `app_asset`, never inside a version.** The turn writes them
  into `public/uploads/` when it opens the project and the packer leaves that
  directory out, so ten corrections to a page keep one copy of the photograph
  rather than eleven. They are added back into the zip on the way out.
- **The agent is told they exist.** The prompt names each one with the path the
  page uses, because an agent left to discover a photograph writes a grey box
  instead.

The console shows them under the composer: clicking one puts `/uploads/name.png`
in the message, which is the whole of "use this picture in the header".

### What a workspace may keep

Three limits, each in the one place it can be enforced:

- **Apps per plan** (`check_app_quota`, in `create_project`): two on Free.
  Counted apart from flows, because a message runs a coding agent and a
  compiler and every build is kept.
- **Storage per plan** (`check_storage_quota`): 100MB on Free, counted over
  every artifact and every upload the workspace holds, and checked in
  `_save_artifact`, which is the single place bytes are written. A turn that
  would go over fails with a sentence naming the number, and the project keeps
  the version it had.
- **Per project**: 8MB of source, 24MB of build, 12MB of images. These stop one
  app spending a whole workspace's allowance.

Disk is the one resource a deployment cannot overcommit, which is why the
storage check is before the write rather than a report afterwards.

### Taking the code away

Every version's source downloads as a zip from the version rail: one folder,
`package.json`, `vite.config.ts`, `src/`, and a README that says `npm install`
and `npm run dev`. It is the whole project as it stood at that version, and it
runs anywhere Node does. Nothing about basivo is in it.

### Cheap to serve

A hundred people opening one app is three hundred asset requests, and reading
a gzipped tar out of Postgres and unpacking it for each of them would make the
database the bottleneck of a static site. So:

- **Once per version per process.** A build is unpacked on first request and
  kept in a bounded in-memory cache (128MB, least recently served evicted).
  Versions are immutable, so there is nothing to invalidate; a new deploy is a
  new artifact id.
- **ETags on everything.** A returning browser gets a 304 and no body.
- **Hashed assets are immutable** wherever they are addressed from, so the
  browser does not ask again for a year. The published `index.html` is the
  one thing a new deploy replaces, and it is the one thing served `no-cache`.
- **Vite's production build** minifies, tree shakes and hashes. `motion` and
  `lucide-react` are in the template because developers ask for them and
  because both tree shake to what the page actually uses.

The remaining cost is one row read per version per API process. When that is
too much, the answer is object storage with a CDN in front, not a bigger
cache here.

## Console

New section, `Apps`, above Flows.

- `/app/apps` project list: name, engine, deployed version, last activity.
- `/app/apps/:id` the builder: chat on the left, preview on the right, a
  version rail under the preview with Deploy, Share and Open.
- **The wait says what the agent is doing.** OpenCode prints an event when a
  tool finishes, so `Reading App.tsx`, `Editing Menu.tsx` and
  `Reading the documentation` are what actually happened, emitted as the run's
  own `node.progress` events and read back by the console while it polls. The
  elapsed clock stays beside it. Nothing here is a timer guessing at stages.
- **Refreshing the page loses nothing.** The turn is a run and the state is in
  Postgres: the reload asks for the project, sees a turn still going, and
  attaches to the same event log. Only unsent text in the box is lost.
- Everything uses `components/fields.tsx`, no native controls, no
  `window.confirm`, and no dashes in copy. The existing rules apply unchanged.

## Phases

| # | What | Done when | |
| --- | --- | --- | --- |
| 0 | OpenCode headless spike: confirm the model id, the auth path and the JSON output shape | one prompt edits one file in a temp dir | done |
| 1 | `CodingEngine` interface, three implementations, `git.autofix` moved onto it, credential made optional for free engines | `pytest` covers engine choice, redaction and the tool limits | done |
| 1b | Docs MCP server, wired into all three engines, tick box on both nodes | a turn that must read current documentation gets the page, and the run log shows the search | done |
| 2 | Worker image: pinned `opencode` and `codex` CLIs beside Claude Code, a warmed OpenCode home, the template, its baked `node_modules`, `AGENTS.md` | `vite build` of the untouched template runs in the image offline, and all three CLIs answer `--version` in the image test | agents done, template to do |
| 3 | Data model, migration, `Workspace`, `app.build` node, project CRUD, the turn lock | an API level test drives two turns, the second correcting the first, and gets two versions | done |
| 4 | Serving: apps origin, tokens, CSP, version and published routes, public slugs, unpublish, code download, the site cache | a published version loads in a browser, a wrong token gives 404, a hundred requests cost one read | done |
| 5 | Console: list, builder, preview, version rail with Undo, Deploy, Share, Unpublish, Download code, starter prompts | a person builds, corrects, undoes and deploys without touching an editor | done, live tested |
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
- **The free model is slow**, thirty seconds to a minute for a small edit and
  several minutes for a whole page. That is fine for a repair and it is the
  reason the App Builder shows what it is doing and how long it has been.
- **The free model goes quiet.** On a demanding first message it stopped
  streaming with the socket open, and no overall timeout notices that for ten
  minutes. Streaming agents print an event per token, so silence is the
  signal: a stall watchdog kills an agent that prints nothing for two
  minutes and says so. And a turn is closed however its agent ends, at the
  node and again by reconciling against the run, because a project that says
  "working" forever is the one failure a person cannot recover from.
- **A sandboxed page is a foreign origin to itself.** With no
  `allow-same-origin` the document's origin is opaque, so its own script and
  stylesheet arrive as cross-origin requests from `null` and need
  `Access-Control-Allow-Origin` or the page renders as a white box with no
  error anyone can see. And a directory address needs its trailing slash, or
  relative asset paths resolve one level too high.

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
   number is. The plan now caps apps and storage; turns themselves are still
   only capped by the runs limit.
