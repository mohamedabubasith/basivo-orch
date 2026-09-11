"""One turn of the App Builder, as a node.

A project's turns are runs. That is not a trick: it is the reason the builder
costs so little to operate. The queue, the claim and retry semantics, the live
event log, per node timeouts, the Runs screen for support and the plan limits
all already exist and all already work, and a second scheduler for "app turns"
would be a second copy of every one of them, diverging from the first.

So each project owns a flow with a single node, this one, hidden from the flows
list. Sending a message creates a run. The console follows that run the same
way the chat page follows its own.

The node holds no database access, like every other node: what it needs from
Postgres arrives through `ctx.app_state` and `ctx.load_artifact`, which the
engine provides. What it does itself is the part that belongs to a node,
namely running a coding agent over a directory and building the result.
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from pydantic import BaseModel, Field

from basivo_orch.appbuilder import turns
from basivo_orch.appbuilder import workspace as ws
from basivo_orch.flows.nodes import engines
from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult


class AppBuildConfig(BaseModel):
    model_config = {"extra": "forbid"}

    #: Which project this node builds. Written when the project is created and
    #: never edited by hand, so it is not shown on the canvas.
    project_id: str = Field(default="", max_length=64, json_schema_extra={"x-hidden": True})

    engine: Literal["auto", "opencode", "claude_code", "codex"] = Field(
        default="auto",
        title="Coding agent",
        json_schema_extra={
            "x-enum-labels": {
                "auto": "Automatic",
                "opencode": "Free agent (no key needed)",
                "claude_code": "Claude Code (Anthropic only)",
                "codex": "Codex (OpenAI only)",
            }
        },
    )
    provider: str = Field(default="anthropic", max_length=48, json_schema_extra={"x-hidden": True})
    model: str = Field(default="", max_length=160, json_schema_extra={"x-hidden": True})
    credential_id: str = Field(
        default="",
        title="Model credential",
        description="Leave empty to use the free agent.",
    )
    search_docs: bool = Field(
        default=True,
        title="Look up documentation on the web",
        description=(
            "Lets the agent read current documentation while it works, so it writes against "
            "the library as it is today."
        ),
    )


class AppBuildNode(Node):
    type = "app.build"
    label = "Build the App"
    description = (
        "Turns one message into a change to an app: a coding agent edits the project, the "
        "page is built, and a version is kept."
    )
    when = (
        "This node is created and maintained by the App Builder. Open a project under Apps "
        "rather than wiring it by hand."
    )
    needs = (
        "A project under Apps, which creates this node for you.",
        "Optional: an LLM credential. Without one the free coding agent does the work.",
    )
    example = "Apps -> a project"
    tier = 2
    category = "devops"
    config_model = AppBuildConfig
    #: A page is compiled here and an agent runs beside it, so one at a time
    #: per worker, exactly like a video render.
    heavy = True
    #: Never on the palette: a project owns its node, and a loose one on a
    #: canvas would have no project to write to.
    hidden = True
    output_paths = ("ok", "reply", "version", "files_changed", "error")

    @classmethod
    def budget_seconds(cls, config: AppBuildConfig) -> float:
        # Two agent turns at the engine's own ceiling, plus two builds, plus
        # the packing either side. The free agent is the slow one and a minute
        # for a small edit is normal for it.
        return float(2 * (turns.AGENT_TIMEOUT_SECONDS + ws.BUILD_TIMEOUT_SECONDS) + 60)

    async def run(self, config: AppBuildConfig, ctx: NodeContext) -> NodeResult:
        if ctx.app_state is None or ctx.load_artifact is None or ctx.save_artifact is None:
            raise NodeError("This node only runs inside the App Builder.")

        payload = (ctx.trigger or {}).get("payload") or {}
        message = str(payload.get("message") or "").strip()
        turn_id = str(payload.get("turn_id") or "")
        if not message or not turn_id:
            raise NodeError("An app turn needs a message. Send it from the project's chat.")

        opened = await ctx.app_state(action="open", project_id=config.project_id, turn_id=turn_id)
        source = None
        if artifact_id := opened.get("source_artifact_id"):
            source = await ctx.load_artifact(str(artifact_id))

        engine, reason = engines.choose(
            config.engine,
            provider=config.provider,
            has_credential=bool(config.credential_id),
        )
        if engine is None:
            # There is no built-in loop here: the builder needs an agent that
            # edits files on disk, and the loop only stages writes in memory.
            raise NodeError(
                "No coding agent is available for this project. Install the free agent on "
                "the worker, or pick a credential the installed agents can use."
            )
        await ctx.step("app.engine", {"engine": engine.name, "reason": reason})

        credential = None
        if not engine.free:
            credential = await ctx.resolve_credential(config.credential_id)
            if credential is None:
                raise NodeError(
                    f"Pick a credential for {engine.label}, or leave it empty to use the "
                    "free agent."
                )

        servers: dict[str, dict[str, Any]] = {}
        allowed: list[str] = []
        if config.search_docs:
            servers["basivo_docs"] = {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "basivo_orch.flows.nodes.docs_mcp"],
            }
            allowed.append("mcp__basivo_docs")

        result = await turns.run_turn(
            message=message,
            history=[(item["prompt"], item.get("reply", "")) for item in opened.get("history", [])],
            source=source,
            engine=engine,
            workspace=ws.TempWorkspace(),
            api_key=credential.api_key if credential else "",
            base_url=credential.base_url if credential else None,
            model=config.model if not engine.free else "",
            timeout_seconds=turns.AGENT_TIMEOUT_SECONDS,
            mcp_servers=servers or None,
            allowed_mcp_tools=tuple(allowed),
            progress=ctx.progress,
            step=ctx.step,
        )

        stored: dict[str, Any] = {}
        if result.ok:
            saved_source = await ctx.save_artifact(
                data=result.source, filename="source.tar.gz", content_type="application/gzip"
            )
            saved_build = await ctx.save_artifact(
                data=result.dist, filename="site.tar.gz", content_type="application/gzip"
            )
            stored = {
                "source_artifact_id": saved_source.get("artifact_id"),
                "build_artifact_id": saved_build.get("artifact_id"),
            }

        finished = await ctx.app_state(
            action="finish",
            project_id=config.project_id,
            turn_id=turn_id,
            ok=result.ok,
            reply=result.reply,
            error=result.error,
            engine=result.engine,
            **stored,
        )

        if not result.ok:
            # A failed turn is a failed run: the person sees why, the project
            # keeps the version it had, and the flow's own error path applies.
            raise NodeError(result.error or "The app could not be built.")

        return NodeResult(
            output={
                "ok": True,
                "reply": result.reply,
                "version": finished.get("version"),
                "files_changed": result.files_changed,
                "error": "",
            },
            metrics={"attempts": result.attempts, "files": len(result.files_changed)},
        )
