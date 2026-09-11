"""One message, turned into a built app.

The first message makes an app and every message after it is a correction, so
a turn is not "generate a project" but "open what exists, change the thing they
named, prove it still builds". Everything here follows from that:

**The tree is the context.** The agent is given the directory and a few lines
of what was asked before, not a transcript. Files are what it reads anyway,
and a short history is enough for "make it smaller" to mean the right thing.

**A failed build gets exactly one more attempt.** The log goes back to the
agent once. A second failure is reported with the error, because a repair loop
that cannot converge is how twelve minutes disappear and the person is left
watching a spinner that will never stop.

**A broken app is never stored.** The project's tree is replaced only when the
build succeeds. A failed turn leaves the last working version exactly where it
was, so the preview beside the chat keeps showing something real.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from basivo_orch.appbuilder import workspace as ws
from basivo_orch.flows.nodes.engines import CodingEngine

#: How many earlier messages the agent is told about. Enough for "make that
#: bigger" to resolve, few enough that turn forty does not carry turn one.
HISTORY_TURNS = 6

#: How much of a failed build the agent is shown. The error is at the end.
BUILD_ERROR_CHARS = 2500


@dataclass
class TurnResult:
    """What one message produced."""

    ok: bool
    reply: str = ""
    error: str = ""
    #: The new tree and the built site, both empty unless the build passed.
    source: bytes = b""
    dist: bytes = b""
    files_changed: list[str] = field(default_factory=list)
    build_log: str = ""
    engine: str = ""
    attempts: int = 1


def compose_prompt(message: str, history: list[tuple[str, str]]) -> str:
    """The task, with just enough of the conversation to make it unambiguous."""
    lines: list[str] = []
    recent = history[-HISTORY_TURNS:]
    if recent:
        lines.append("Earlier in this conversation:")
        for asked, answered in recent:
            lines.append(f"- They asked: {asked.strip()[:300]}")
            if answered.strip():
                lines.append(f"  You replied: {answered.strip()[:200]}")
        lines.append("")
        lines.append("The project already contains that work. Read the files before changing them.")
        lines.append("")
    lines.append("They now say:")
    lines.append(message.strip())
    return "\n".join(lines)


def repair_prompt(log: str) -> str:
    """What the agent is told when its own change will not build."""
    return (
        "Your change does not build. This is the output, ending with the error:\n\n"
        f"{log[-BUILD_ERROR_CHARS:]}\n\n"
        "Fix the cause in the files you changed. Do not start again, do not remove the "
        "feature to make the error go away, and do not add a dependency."
    )


async def run_turn(
    *,
    message: str,
    history: list[tuple[str, str]],
    source: bytes | None,
    engine: CodingEngine,
    workspace: ws.Workspace,
    api_key: str = "",
    base_url: str | None = None,
    model: str = "",
    timeout_seconds: float = 600.0,
    mcp_servers: dict[str, dict[str, Any]] | None = None,
    allowed_mcp_tools: tuple[str, ...] = (),
    progress: Callable[[str], Awaitable[None]] | None = None,
    step: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
) -> TurnResult:
    """Open the project, let the agent change it, build it, hand back the result."""

    async def say(text: str) -> None:
        if progress:
            await progress(text)

    async def record(kind: str, data: dict[str, Any]) -> None:
        if step:
            await step(kind, data)

    root = await workspace.open(source)
    try:
        before = ws.snapshot(root)
        await record("app.opened", {"files": len(before), "first_turn": source is None})

        await say("Reading the project" if source else "Starting the project")
        result = await engine.run(
            cwd=root,
            prompt=compose_prompt(message, history),
            system_prompt="",  # the rules are AGENTS.md, which every engine reads
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            mcp_servers=mcp_servers,
            allowed_mcp_tools=allowed_mcp_tools,
        )
        reply = result.text.strip()
        after = ws.snapshot(root)

        refused = ws.refused_writes(before, after)
        if refused:
            # Not dropped silently: a change half applied is worse than none,
            # and the person is owed the reason.
            return TurnResult(
                ok=False,
                engine=engine.name,
                reply=reply,
                error=(
                    "That change touched files this builder does not let an agent edit: "
                    + ", ".join(refused[:5])
                    + ". Ask for the change in the page itself."
                ),
            )

        changed = sorted(path for path, blob in after.items() if before.get(path) != blob)
        changed += sorted(path for path in before if path not in after)
        if not changed:
            return TurnResult(
                ok=False,
                engine=engine.name,
                reply=reply,
                error=(
                    "Nothing changed. Say what you want different in the page, and be "
                    "specific about which part."
                ),
            )
        await record("app.changed", {"files": changed[:20], "count": len(changed)})

        await say("Building the page")
        build = await workspace.build(root)
        attempts = 1

        if not build.ok:
            # One more, with the error. Once only.
            attempts = 2
            await record("app.build_failed", {"seconds": round(build.seconds, 1)})
            await say("The build failed, the agent is fixing it")
            result = await engine.run(
                cwd=root,
                prompt=repair_prompt(build.log),
                system_prompt="",
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
                mcp_servers=mcp_servers,
                allowed_mcp_tools=allowed_mcp_tools,
            )
            reply = result.text.strip() or reply
            after = ws.snapshot(root)
            if refused := ws.refused_writes(before, after):
                return TurnResult(
                    ok=False,
                    engine=engine.name,
                    reply=reply,
                    error="The fix touched files it may not edit: " + ", ".join(refused[:5]) + ".",
                    attempts=attempts,
                )
            build = await workspace.build(root)

        if not build.ok:
            return TurnResult(
                ok=False,
                engine=engine.name,
                reply=reply,
                error="The page does not build. " + _first_error(build.log),
                build_log=build.log,
                attempts=attempts,
            )

        await record(
            "app.built",
            {"seconds": round(build.seconds, 1), "bytes": len(build.dist), "attempts": attempts},
        )
        return TurnResult(
            ok=True,
            engine=engine.name,
            reply=reply,
            source=await workspace.close(root),
            dist=build.dist,
            files_changed=changed,
            build_log=build.log,
            attempts=attempts,
        )
    finally:
        # `close` already deleted it on the success path, and `discard` is a
        # no-op then. Every other path, including a raised error, lands here.
        await workspace.discard(root)


def _first_error(log: str) -> str:
    """The line a person can act on, out of a build log written for a terminal."""
    for line in log.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("error", "x [error]", "[vite]")) and len(stripped) > 12:
            return stripped[:300]
    return log.strip().splitlines()[-1][:300] if log.strip() else "No output from the build."
