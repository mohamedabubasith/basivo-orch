"""Which coding agent does the work, behind one interface.

Three command line agents can edit a working copy for us: Claude Code, Codex
and OpenCode. They are not interchangeable in quality or in price, but they are
interchangeable in shape: hand one a directory, a task and a system prompt, and
it edits files until it is done. That shape is this module.

The reason it exists rather than a branch in the repair node: OpenCode's free
model means a workspace with no LLM credential at all can still fix a bug and
build an app. Making that true in one place, instead of once per node, is the
whole point of an interface.

Four rules hold for every engine here, and they are the safety story:

**No shell.** Every engine runs with file tools only. Claude Code refuses Bash
by flag, OpenCode by disabling the tool on its agent, Codex by an OS sandbox
with no network. A coding agent that can run commands on the worker is a remote
shell for whoever wrote the bug report it is reading.

**No host configuration.** Each engine gets a throwaway HOME and is told to
ignore the worker's own settings, so one tenant's run cannot be shaped by
another's leftovers or by whatever the operator has configured for themselves.

**The credential is an environment variable and nothing else.** Never on the
command line, which is public to every process on the machine, and scrubbed out
of anything this module raises or logs.

**We compute the diff, the agent does not report it.** `claude_code.snapshot`
before and after is the truth; an agent's own account of what it changed is a
claim, and the protected path check has to run against facts.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from basivo_orch.flows.nodes import claude_code, jail
from basivo_orch.flows.nodes.base import NodeError
from basivo_orch.logging import get_logger

log = get_logger(__name__)

#: The free model everyone gets. OpenCode Zen serves it without an account, so
#: a new workspace can run a repair before it has saved a single credential.
#: Overridable because a stealth model's name changes and a deployment that
#: pays for something better should be able to say so.
DEFAULT_OPENCODE_MODEL = os.environ.get("BASIVO_OPENCODE_MODEL", "opencode/big-pickle")

#: A prewarmed OpenCode home, baked into the worker image, holding `data` and
#: `config`. Every run gets a throwaway HOME by design, and a cold one costs
#: two things: a one time sqlite migration, and a package install OpenCode
#: performs on its first real session.
OPENCODE_HOME_TEMPLATE = os.environ.get("BASIVO_OPENCODE_HOME_TEMPLATE", "")

#: Where the worker keeps what the image could not warm.
#:
#: The image warms the home at build time, but the package half needs a real
#: session, which needs a model to answer, and a build that depends on a third
#: party's endpoint is a build that fails for reasons nobody in the room can
#: fix. So the worker finishes the job on its first turn: that turn is already
#: talking to a model, so the network is known to work, and what it installs
#: is copied here for every turn after it.
#:
#: Written by the worker and never by an agent, which cannot see this path
#: from inside its jail. That is what makes sharing it safe.
OPENCODE_WARM_CACHE = os.environ.get("BASIVO_OPENCODE_WARM_CACHE", "/tmp/basivo-opencode-warm")  # noqa: S108

#: An agent's task can be a whole bug report. Past this it does not travel as
#: an argument: ARG_MAX is a real limit and a visible command line is a real
#: leak. Engines that cannot read stdin get the task in a file instead.
MAX_ARGV_PROMPT = 60_000

#: How long a streaming agent may say nothing before it is presumed gone.
#:
#: The agents here print an event for every model token and every tool call,
#: so a healthy run is never silent for long: the gaps are a model thinking,
#: seconds at most. Seven silent minutes means the provider is holding a
#: socket open and sending nothing, which no overall timeout notices until it
#: is far too late, and which the person watching experiences as a spinner
#: that never stops. Stalls are killed here, quickly, and reported as what
#: they are.
STALL_SECONDS = 120.0


@dataclass
class EngineResult:
    """What an engine did, in the fields the run log records.

    Deliberately the same shape as `claude_code.ClaudeCodeResult`: the repair
    node already writes these to a run step, and an engine that reports no cost
    reports zero rather than inventing one.
    """

    text: str
    cost_usd: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    session_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


#: One MCP server as every engine will be asked to describe it: a local process
#: speaking stdio, or a remote URL. This is the canonical form; each engine
#: translates it into its own configuration file.
McpServers = dict[str, dict[str, Any]]


class CodingEngine(Protocol):
    """What a command line coding agent must be able to do for us."""

    #: Stored on the node's configuration and on the run log.
    name: str
    #: Shown to a person. Says whether it is free; never names the model.
    label: str
    #: True when the operator pays, which is what makes metering necessary.
    free: bool

    def available(self) -> bool:
        """Is the binary installed on this worker."""
        ...

    def drives(self, provider: str) -> bool:
        """Can it run on a credential from this provider."""
        ...

    async def run(
        self,
        *,
        cwd: Path,
        prompt: str,
        system_prompt: str,
        api_key: str = "",
        base_url: str | None = None,
        model: str = "",
        max_turns: int = 30,
        max_budget_usd: float | None = None,
        timeout_seconds: float = 780.0,
        mcp_servers: McpServers | None = None,
        allowed_mcp_tools: Sequence[str] = (),
        on_activity: Callable[[str], Awaitable[None]] | None = None,
    ) -> EngineResult:
        """One headless session over `cwd`, editing files in place.

        `allowed_mcp_tools` holds `mcp__server` and `mcp__server__tool`
        patterns. An engine that cannot express a per tool limit refuses the
        run rather than quietly granting the whole server.
        """
        ...


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


async def _execute(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin: bytes | None,
    timeout_seconds: float,
    secret: str,
    engine: str,
    stall_seconds: float | None = STALL_SECONDS,
    on_line: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[int, str, str]:
    """Run a CLI to completion, or kill it. Returns (code, stdout, stderr).

    Two clocks. The overall timeout is the node's ceiling for the whole task.
    The stall clock is the sharper one: it restarts on every byte the agent
    prints, and a streaming agent that prints nothing for `stall_seconds` has
    stopped, whatever its socket says. Pass `None` for an agent that only
    speaks at the end, where silence means nothing.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None

    async def feed() -> None:
        if stdin is not None and process.stdin is not None:
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()

    out = bytearray()
    err = bytearray()
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    async def pump(
        stream: asyncio.StreamReader,
        into: bytearray,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        # Chunks, not lines: a line that never ends must still count as life.
        # The line splitting on top of that is for the caller who wants to know
        # what the agent is doing while it is still doing it.
        tail = b""
        while chunk := await stream.read(65536):
            into.extend(chunk)
            if notify is None:
                continue
            *lines, tail = (tail + chunk).split(b"\n")
            for line in lines:
                with suppress(Exception):
                    # Reporting progress must never be able to fail a run.
                    await notify(line.decode(errors="replace"))
            if len(tail) > 1_000_000:
                tail = b""  # a line nobody is going to finish

    async def drain() -> None:
        await asyncio.gather(pump(process.stdout, out, on_line), pump(process.stderr, err))

    async def kill(reason: str) -> None:
        process.kill()
        with suppress(ProcessLookupError):
            await process.wait()
        raise NodeError(reason)

    feeder = asyncio.create_task(feed())
    reader = asyncio.create_task(drain())
    try:
        while not reader.done():
            remaining = deadline - asyncio.get_running_loop().time()
            seen = len(out) + len(err)
            window = remaining if stall_seconds is None else min(remaining, stall_seconds)
            done, _ = await asyncio.wait({reader}, timeout=max(0.0, window))
            if done:
                break
            # The ceiling first: at the deadline the honest word is "too long",
            # even if the last thing the agent did was fall silent.
            if asyncio.get_running_loop().time() >= deadline:
                await kill(
                    f"{engine} did not finish within {timeout_seconds:.0f}s. Narrow the task, "
                    "or raise the node's timeout."
                )
            if stall_seconds is not None and len(out) + len(err) == seen:
                await kill(
                    f"{engine} stopped responding: nothing for {stall_seconds:.0f} seconds "
                    "while it was working. The model provider has gone quiet; send the "
                    "message again."
                )
        await reader
        await feeder
        await process.wait()
    finally:
        for task in (feeder, reader):
            if not task.done():
                task.cancel()
    return (
        process.returncode or 0,
        claude_code.redact(bytes(out).decode(errors="replace"), secret),
        claude_code.redact(bytes(err).decode(errors="replace"), secret),
    )


def _base_env(home: Path, secret_name: str = "", secret: str = "") -> dict[str, str]:
    """A minimal environment: PATH, a throwaway HOME, no telemetry, no colour.

    Nothing is inherited beyond PATH. A CLI that reads `ANTHROPIC_BASE_URL` or
    `OPENAI_API_KEY` from the worker's own environment would silently run on
    the operator's account instead of the tenant's.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CI": "1",
    }
    if secret_name and secret:
        env[secret_name] = secret
    return env


def _refuse_per_tool_limits(allowed: Sequence[str], label: str) -> None:
    """Only Claude Code can allow a single tool of an MCP server.

    The others take a server or leave it. Widening a restricted server to all
    of its tools because the engine changed would be a permission a person
    never granted, so the run stops instead.
    """
    narrowed = sorted({pattern.split("__")[1] for pattern in allowed if pattern.count("__") >= 2})
    if narrowed:
        raise NodeError(
            f"{label} cannot limit an MCP server to particular tools, and "
            + ", ".join(narrowed)
            + " is restricted that way. Clear the tool list on that server, or run this node "
            "on Claude Code."
        )


# ---------------------------------------------------------------------------
# The engines
# ---------------------------------------------------------------------------


class ClaudeCodeEngine:
    """The strongest of the three, on the workspace's own Anthropic credential."""

    name = "claude_code"
    label = "Claude Code (Anthropic)"
    free = False

    def available(self) -> bool:
        return claude_code.binary() is not None

    def drives(self, provider: str) -> bool:
        return provider == "anthropic"

    async def run(
        self,
        *,
        cwd: Path,
        prompt: str,
        system_prompt: str,
        api_key: str = "",
        base_url: str | None = None,
        model: str = "",
        max_turns: int = 30,
        max_budget_usd: float | None = None,
        timeout_seconds: float = 780.0,
        mcp_servers: McpServers | None = None,
        allowed_mcp_tools: Sequence[str] = (),
        on_activity: Callable[[str], Awaitable[None]] | None = None,
    ) -> EngineResult:
        allowed = list(allowed_mcp_tools) or [f"mcp__{name}" for name in (mcp_servers or {})]
        result = await claude_code.run_claude_code(
            cwd=cwd,
            prompt=prompt,
            system_prompt=system_prompt,
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            timeout_seconds=timeout_seconds,
            mcp_config={"mcpServers": mcp_servers} if mcp_servers else None,
            extra_allowed_tools=allowed,
        )
        return EngineResult(
            text=result.text,
            cost_usd=result.cost_usd,
            turns=result.turns,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=result.duration_ms,
            session_id=result.session_id,
            raw=result.raw,
        )


class CodexEngine:
    """OpenAI's agent, on the workspace's own OpenAI credential.

    Two differences from the others are worth knowing when reading the argv
    below. Codex has no flag for an extra system prompt, so the house rules
    ride at the top of the task text. And its shell tool cannot be removed, so
    it is contained instead: an OS sandbox that can write only inside the
    working copy, with network access off.
    """

    name = "codex"
    label = "Codex (OpenAI)"
    free = False

    def available(self) -> bool:
        return bool(os.environ.get("BASIVO_CODEX_BIN") or shutil.which("codex"))

    def drives(self, provider: str) -> bool:
        return provider == "openai"

    async def run(
        self,
        *,
        cwd: Path,
        prompt: str,
        system_prompt: str,
        api_key: str = "",
        base_url: str | None = None,
        model: str = "",
        max_turns: int = 30,
        max_budget_usd: float | None = None,
        timeout_seconds: float = 780.0,
        mcp_servers: McpServers | None = None,
        allowed_mcp_tools: Sequence[str] = (),
        on_activity: Callable[[str], Awaitable[None]] | None = None,
    ) -> EngineResult:
        import tempfile

        _refuse_per_tool_limits(allowed_mcp_tools, self.label)
        executable = os.environ.get("BASIVO_CODEX_BIN") or shutil.which("codex")
        if executable is None:
            raise NodeError(
                "Codex is not installed on this worker. The worker image installs it as "
                "`@openai/codex`; set BASIVO_CODEX_BIN if it lives elsewhere."
            )

        with tempfile.TemporaryDirectory(prefix="basivo-codex-home-") as home_dir:
            home = Path(home_dir)
            last_message = home / "last.txt"
            argv = [
                executable,
                "exec",
                # The task arrives on stdin: it carries a whole bug report, and
                # an argument that long is both visible in the process table
                # and a way to meet ARG_MAX.
                "-",
                "--json",
                "--cd",
                str(cwd),
                # Writes land in the working copy; the model's shell gets no
                # network, so a command it invents cannot reach anything.
                "--sandbox",
                "workspace-write",
                "-c",
                "sandbox_workspace_write.network_access=false",
                # The tree is an extracted archive, not a checkout.
                "--skip-git-repo-check",
                # Never the operator's own configuration, hooks or rules.
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--color",
                "never",
                "-o",
                str(last_message),
            ]
            if model:
                argv += ["-m", model]
            for server, spec in (mcp_servers or {}).items():
                argv += _codex_mcp_overrides(server, spec)

            env = _base_env(home, "OPENAI_API_KEY", api_key)
            env["CODEX_HOME"] = str(home)
            if base_url:
                env["OPENAI_BASE_URL"] = base_url

            task = f"{system_prompt}\n\n---\n\n{prompt}" if system_prompt else prompt
            code, out, err = await _execute(
                jail.wrap(argv, workspace=cwd, home=home),
                cwd=cwd,
                env=env,
                stdin=task.encode(),
                timeout_seconds=timeout_seconds,
                secret=api_key,
                engine="Codex",
                stall_seconds=STALL_SECONDS,
            )
            text = ""
            if last_message.exists():
                text = claude_code.redact(last_message.read_text(errors="replace"), api_key)

        events = _jsonl(out)
        if code != 0 and not text:
            raise NodeError(
                f"Codex exited with status {code}: {(err or out).strip()[-600:] or 'no output'}"
            )
        usage = _codex_usage(events)
        return EngineResult(
            text=text.strip() or _codex_last_text(events),
            turns=sum(1 for event in events if event.get("type", "").endswith("command_execution")),
            input_tokens=usage[0],
            output_tokens=usage[1],
            raw={"events": len(events)},
        )


class OpenCodeEngine:
    """Free for everyone, which is why it is the default.

    OpenCode Zen serves a coding model without an account, so this engine needs
    no credential from the workspace at all. That spend is the operator's, so
    the node that uses it meters the free engine and not the others.

    Two things learned the hard way and encoded below: `permission: deny` makes
    a headless run hang waiting for an approval nobody can give, so tools are
    removed from the agent instead; and a fresh data directory pays a one time
    database migration measured in minutes, so the image prewarms one and each
    run gets a copy.
    """

    name = "opencode"
    label = "OpenCode (free)"
    free = True

    def available(self) -> bool:
        return bool(os.environ.get("BASIVO_OPENCODE_BIN") or shutil.which("opencode"))

    def drives(self, provider: str) -> bool:
        # It brings its own model. Whatever credential the node holds is
        # irrelevant to it, which is the point of the free engine.
        return True

    async def run(
        self,
        *,
        cwd: Path,
        prompt: str,
        system_prompt: str,
        api_key: str = "",
        base_url: str | None = None,
        model: str = "",
        max_turns: int = 30,
        max_budget_usd: float | None = None,
        timeout_seconds: float = 780.0,
        mcp_servers: McpServers | None = None,
        allowed_mcp_tools: Sequence[str] = (),
        on_activity: Callable[[str], Awaitable[None]] | None = None,
    ) -> EngineResult:
        import tempfile

        _refuse_per_tool_limits(allowed_mcp_tools, self.label)
        executable = os.environ.get("BASIVO_OPENCODE_BIN") or shutil.which("opencode")
        if executable is None:
            raise NodeError(
                "OpenCode is not installed on this worker. The worker image installs it; "
                "set BASIVO_OPENCODE_BIN if it lives elsewhere."
            )

        with tempfile.TemporaryDirectory(prefix="basivo-opencode-home-") as home_dir:
            home = Path(home_dir)
            config = home / "opencode.json"
            # Copying a warmed data directory is real file work; off the event
            # loop, because the worker is running other flows meanwhile.
            await asyncio.to_thread(
                _write_opencode_home, home, _opencode_config(system_prompt, mcp_servers)
            )

            # The task rides as an argument, so it is capped. Nothing secret is
            # in it (the credential never enters a prompt), but ARG_MAX is real.
            task = prompt
            if len(task) > MAX_ARGV_PROMPT:
                task = task[:MAX_ARGV_PROMPT] + "\n[truncated]"
            argv = [
                executable,
                "run",
                # No plugins from the host, no shared session, JSON events.
                "--pure",
                "--dir",
                str(cwd),
                "--format",
                "json",
                "-m",
                model or DEFAULT_OPENCODE_MODEL,
                task,
            ]
            env = _base_env(home)
            env["OPENCODE_CONFIG"] = str(config)
            env["TMPDIR"] = str(home / "tmp")
            if api_key:
                env["OPENCODE_API_KEY"] = api_key

            code, out, err = await _execute(
                jail.wrap(argv, workspace=cwd, home=home),
                cwd=cwd,
                env=env,
                stdin=None,
                timeout_seconds=timeout_seconds,
                secret=api_key,
                engine="OpenCode",
                stall_seconds=STALL_SECONDS,
                on_line=_narrator(on_activity) if on_activity else None,
            )
            await asyncio.to_thread(_keep_what_was_warmed, home)

        events = _jsonl(out)
        text = _opencode_text(events)
        if code != 0 and not text:
            raise NodeError(
                f"OpenCode exited with status {code}: {(err or out).strip()[-600:] or 'no output'}"
            )
        spent = _opencode_usage(events)
        return EngineResult(
            text=text,
            cost_usd=spent[2],
            input_tokens=spent[0],
            output_tokens=spent[1],
            turns=sum(1 for event in events if event.get("type") == "tool_use"),
            session_id=next(
                (
                    str(event.get("part", {}).get("sessionID") or "")
                    for event in events
                    if event.get("part", {}).get("sessionID")
                ),
                "",
            ),
            raw={"events": len(events)},
        )


# ---------------------------------------------------------------------------
# Per engine translation
# ---------------------------------------------------------------------------


def _packages_in(home: Path) -> Path | None:
    """Where OpenCode put the packages it installs for itself, if it has."""
    candidate = home / "config" / "opencode" / "node_modules"
    return candidate if candidate.is_dir() else None


def _write_opencode_home(home: Path, config: dict[str, Any]) -> None:
    """Lay down a HOME OpenCode can start in without paying to warm it.

    Both halves of the warmed home are *copied*, not shared. `data` holds the
    database and the session history, which are a tenant's. `config` holds the
    packages OpenCode installs for itself, and a shared, writable copy of those
    would let one tenant's agent drop a plugin that runs inside the next
    tenant's session.

    The image warms both at build time when it can. When it could not, the
    cache filled by the first successful turn stands in. With neither, the
    directories are simply fresh and this run pays for warming them, which is
    slow exactly once.
    """
    sources = [Path(p) for p in (OPENCODE_HOME_TEMPLATE, OPENCODE_WARM_CACHE) if p]
    for half in ("data", "config"):
        for source in sources:
            if (source / half).is_dir():
                shutil.copytree(source / half, home / half, dirs_exist_ok=True, symlinks=True)
                break
    (home / "tmp").mkdir(exist_ok=True)
    path = home / "opencode.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)


def _keep_what_was_warmed(home: Path) -> None:
    """Copy this run's packages into the cache, if nothing has yet.

    Called after a turn, in a thread, and never allowed to fail one: this is
    an optimisation for the next run, and a run that has already produced an
    answer must not be lost to a full disk while tidying up.
    """
    packages = _packages_in(home)
    if packages is None:
        return
    for source in (OPENCODE_HOME_TEMPLATE, OPENCODE_WARM_CACHE):
        if source and _packages_in(Path(source)) is not None:
            return
    cache = Path(OPENCODE_WARM_CACHE)
    try:
        staging = cache.with_name(f"{cache.name}.{os.getpid()}")
        shutil.rmtree(staging, ignore_errors=True)
        for half in ("data", "config"):
            if (home / half).is_dir():
                shutil.copytree(home / half, staging / half, symlinks=True)
        if cache.exists():
            # Another worker got there first. Theirs is as good as ours.
            shutil.rmtree(staging, ignore_errors=True)
            return
        # One rename, so a half copied cache is never what the next run finds.
        staging.replace(cache)
    except OSError as exc:  # pragma: no cover - a tidy-up, never load bearing
        log.warning("opencode.warm_cache_failed", error=str(exc)[:200])
    else:
        log.info("opencode.warm_cached", path=str(cache))


def _opencode_config(system_prompt: str, mcp_servers: McpServers | None) -> dict[str, Any]:
    """OpenCode's configuration file: no shell, no web, our system prompt.

    `tools: {bash: false}` removes the tool. The alternative, a `permission`
    block set to deny, leaves the tool present and asks for an approval that a
    headless run can never answer, so the process hangs until its timeout.
    """
    agent: dict[str, Any] = {
        "tools": {"bash": False, "webfetch": False, "task": False, "patch": True},
    }
    if system_prompt:
        agent["prompt"] = system_prompt
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {"build": agent},
    }
    servers: dict[str, Any] = {}
    for name, spec in (mcp_servers or {}).items():
        if spec.get("command"):
            servers[name] = {
                "type": "local",
                "command": [spec["command"], *spec.get("args", [])],
                "environment": spec.get("env", {}),
                "enabled": True,
            }
        elif spec.get("url"):
            servers[name] = {
                "type": "remote",
                "url": spec["url"],
                "headers": spec.get("headers", {}),
                "enabled": True,
            }
    if servers:
        config["mcp"] = servers
    return config


def _codex_mcp_overrides(name: str, spec: dict[str, Any]) -> list[str]:
    """Codex takes its MCP servers as `-c` overrides parsed as TOML."""
    if spec.get("command"):
        overrides = ["-c", f"mcp_servers.{name}.command={json.dumps(spec['command'])}"]
        if spec.get("args"):
            overrides += ["-c", f"mcp_servers.{name}.args={json.dumps(spec['args'])}"]
        if spec.get("env"):
            overrides += ["-c", f"mcp_servers.{name}.env={json.dumps(spec['env'])}"]
        return overrides
    if spec.get("url"):
        overrides = ["-c", f"mcp_servers.{name}.url={json.dumps(spec['url'])}"]
        if spec.get("headers"):
            overrides += ["-c", f"mcp_servers.{name}.http_headers={json.dumps(spec['headers'])}"]
        return overrides
    return []


def _jsonl(output: str) -> list[dict[str, Any]]:
    """Every JSON object in a JSONL stream, skipping whatever else is printed."""
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _opencode_text(events: list[dict[str, Any]]) -> str:
    """OpenCode's reply: the last thing it said.

    A run emits a text part every time the model speaks between tool calls,
    and most of those are it talking to itself ("let me check the types").
    The person asked a question and the answer is the closing message, so
    that is the one that reaches them.
    """
    said = [
        str(event.get("part", {}).get("text") or "").strip()
        for event in events
        if event.get("part", {}).get("type") == "text"
    ]
    spoken = [part for part in said if part]
    return spoken[-1] if spoken else ""


def _opencode_usage(events: list[dict[str, Any]]) -> tuple[int, int, float]:
    """Input tokens, output tokens and cost, summed over the run's steps.

    Every model call ends in a `step_finish` carrying both, so this is the
    engine reporting what it actually spent rather than an estimate. On the
    free model the cost is zero and the tokens are not, which is exactly what
    metering the free tier will need.
    """
    given = taken = 0
    cost = 0.0
    for event in events:
        part = event.get("part") or {}
        if part.get("type") != "step-finish":
            continue
        tokens = part.get("tokens") or {}
        given += int(tokens.get("input") or 0)
        taken += int(tokens.get("output") or 0)
        cost += float(part.get("cost") or 0.0)
    return given, taken, round(cost, 6)


#: What each of OpenCode's tools is called in a sentence a person reads. The
#: agent's own names are for a terminal; this is beside a chat message.
_TOOL_WORDS = {
    "read": "Reading",
    "edit": "Editing",
    "write": "Writing",
    "patch": "Editing",
    "multiedit": "Editing",
}
_LOOKING = ("glob", "grep", "list", "ls")
_SEARCHING = ("webfetch", "websearch", "search_the_web", "read_page")


def _activity(event: dict[str, Any]) -> str:
    """One event as the line the person watching should see, or nothing.

    OpenCode prints an event when a tool finishes and when the model speaks,
    so this is the honest account of what is happening: the file being edited,
    the documentation being read, the sentence the agent just wrote. Anything
    this does not recognise produces no line rather than a guess.
    """
    part = event.get("part") or {}
    kind = part.get("type")

    if kind == "text":
        said = str(part.get("text") or "").strip().replace("\n", " ")
        return said[:120] if said else ""

    if kind == "reasoning":
        return "Thinking"

    if kind != "tool":
        return ""

    tool = str(part.get("tool") or "").lower()
    target = (part.get("state") or {}).get("input") or {}
    name = str(target.get("filePath") or target.get("path") or "").rsplit("/", 1)[-1]

    if word := _TOOL_WORDS.get(tool):
        return f"{word} {name}" if name else f"{word} a file"
    if any(hint in tool for hint in _SEARCHING):
        return "Reading the documentation"
    if any(tool.endswith(hint) or tool == hint for hint in _LOOKING):
        return "Looking through the project"
    if "todo" in tool:
        return "Planning the work"
    return ""


def _narrator(
    report: Callable[[str], Awaitable[None]],
) -> Callable[[str], Awaitable[None]]:
    """A line handler that turns the agent's event stream into progress.

    One line per thing the agent did, which is one run event per tool call:
    the same order of magnitude the agent node already writes, and the reason
    the person watching sees the files go by instead of a spinner. Repeats of
    the line already showing are dropped, because "Reading" three files in a
    row is one thing happening.
    """
    last = ""

    async def handle(line: str) -> None:
        nonlocal last
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or not (text := _activity(event)) or text == last:
            return
        last = text
        await report(text)

    return handle


def _codex_last_text(events: list[dict[str, Any]]) -> str:
    """Codex's closing message, when the last message file was not written."""
    for event in reversed(events):
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            return str(item["text"]).strip()
        if event.get("type") == "agent_message" and event.get("message"):
            return str(event["message"]).strip()
    return ""


def _codex_usage(events: list[dict[str, Any]]) -> tuple[int, int]:
    """Input and output tokens, from the last usage event that carried them."""
    for event in reversed(events):
        usage = event.get("usage") or (event.get("item") or {}).get("usage") or {}
        if usage:
            return (
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            )
    return (0, 0)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

ENGINES: dict[str, CodingEngine] = {
    engine.name: engine
    for engine in (ClaudeCodeEngine(), CodexEngine(), OpenCodeEngine())  # type: ignore[list-item]
}

#: The provider each paid engine is built for. Codex is not in `auto`'s path
#: on purpose: a flow that has been running on the built-in loop with an
#: OpenAI credential should not change engine because the worker was rebuilt.
#: It is chosen, not inherited.
AUTO_PAID = {"anthropic": "claude_code"}


def get(name: str) -> CodingEngine:
    engine = ENGINES.get(name)
    if engine is None:
        raise NodeError(f"Unknown coding engine {name!r}.")
    return engine


def choose(
    requested: str, *, provider: str, has_credential: bool
) -> tuple[CodingEngine | None, str]:
    """The engine to run and the reason, in words that go on the run log.

    Returns `(None, reason)` when the answer is the built-in tool calling loop,
    which is not a CLI and so is not an engine here.

    What `auto` means, in order: the free agent when the node names no
    credential at all, then the CLI built for the credential it does name, then
    the built-in loop. The first line is the one that matters, because it is
    what lets a new account fix a bug before it has a key.
    """
    if requested == "builtin":
        return None, "chosen on the node"

    if requested != "auto":
        engine = get(requested)
        if not engine.available():
            raise NodeError(
                f"{engine.label} is not installed on this worker. The worker image installs "
                "all three coding agents; a custom image needs them too."
            )
        if not engine.free:
            if not has_credential:
                raise NodeError(
                    f"{engine.label} needs a credential. Pick one on this node, or choose "
                    "the free agent."
                )
            if not engine.drives(provider):
                raise NodeError(
                    f"{engine.label} cannot run on a {provider} credential. Pick a matching "
                    "credential, or choose the free agent."
                )
        return engine, "chosen on the node"

    if not has_credential:
        free = ENGINES["opencode"]
        if free.available():
            return free, "no credential saved, so the free agent"
        # No free agent on this worker: carry on to what this node did before
        # there was one, rather than refusing to run.

    preferred = AUTO_PAID.get(provider)
    if preferred is None:
        return None, f"{provider} cannot drive Claude Code"
    engine = ENGINES[preferred]
    name = engine.label.split(" (")[0]
    if engine.available():
        return engine, f"{provider.capitalize()} credential, {name} installed"
    return None, f"{name} is not installed on this worker"
