"""The product's headline pair: raise a ticket, then fix the code and open the PR.

Two Tier-2 nodes, built as nodes on purpose — the whole point of an
orchestrator is that "on failure, file an issue, have an agent repair the code
and open a merge request" is a *flow* someone draws, not a hard-coded feature:

    webhook (alert) → condition → git.ticket → git.autofix

**Everything goes through the host's HTTP API — there is no clone.** Reading
the tree, reading files, creating a branch, committing, opening the PR/MR are
all API calls on `ctx.http`. That is a deliberate trade: no git binary, no
repository on the orchestrator's disk, no new sandbox surface beyond the HTTP
client every node already has — and the entire node is testable against a
mock transport. The cost is scale (a monorepo with tens of thousands of files
wants a real checkout); that is a v2 with its own isolation story, not a
corner to cut silently here.

**The fix loop is the Agent machinery pointed at a repository.** The model
gets four tools — list_files, read_file, write_file, and its own reasoning —
where writes are *staged in memory*, never sent anywhere until the loop ends.
Only then does the node create a branch, commit the staged files, and open
the PR/MR. A model that goes off the rails mid-loop therefore cannot leave a
half-pushed branch behind: the repository is untouched until there is a
complete proposed change, and the change lands on a branch for a human to
review — this node opens pull requests, it does not merge them.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.tools import Tool
from pydantic_ai.usage import UsageLimits

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

VcsProvider = Literal["github", "gitlab"]


# ---------------------------------------------------------------------------
# Repo clients
# ---------------------------------------------------------------------------


class RepoClient:
    """One interface over both hosts, so the nodes never branch on provider."""

    def __init__(self, http: httpx.AsyncClient, *, token: str, base_url: str, repo: str) -> None:
        self.http = http
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.repo = repo

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = await self.http.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            # The host's own error text is the useful part; a run log that
            # says only "422" sends someone to the provider's docs blind.
            raise NodeError(f"{method} {url} → {response.status_code}: {response.text[:300]}")
        return response

    def _headers(self) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    async def branch_sha(self, branch: str) -> str:
        raise NotImplementedError

    async def list_paths(self, ref: str) -> list[str]:
        raise NotImplementedError

    async def read_file(self, path: str, ref: str) -> str:
        raise NotImplementedError

    async def create_branch(self, name: str, from_sha: str) -> None:
        raise NotImplementedError

    async def commit_files(self, branch: str, files: dict[str, str], message: str) -> None:
        raise NotImplementedError

    async def open_pull_request(
        self, branch: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        raise NotImplementedError


class GitHubClient(RepoClient):
    """api.github.com (or a GitHub Enterprise base URL). `repo` is owner/name."""

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def _api(self) -> str:
        return f"{self.base_url}/repos/{self.repo}"

    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        response = await self._request(
            "POST", f"{self._api}/issues", json={"title": title, "body": body, "labels": labels}
        )
        data = response.json()
        return {"url": data["html_url"], "number": data["number"]}

    async def branch_sha(self, branch: str) -> str:
        response = await self._request("GET", f"{self._api}/git/ref/heads/{branch}")
        return response.json()["object"]["sha"]

    async def list_paths(self, ref: str) -> list[str]:
        response = await self._request("GET", f"{self._api}/git/trees/{ref}?recursive=1")
        return [item["path"] for item in response.json().get("tree", []) if item["type"] == "blob"]

    async def read_file(self, path: str, ref: str) -> str:
        response = await self._request("GET", f"{self._api}/contents/{path}?ref={ref}")
        return base64.b64decode(response.json()["content"]).decode("utf-8", errors="replace")

    async def create_branch(self, name: str, from_sha: str) -> None:
        await self._request(
            "POST", f"{self._api}/git/refs", json={"ref": f"refs/heads/{name}", "sha": from_sha}
        )

    async def commit_files(self, branch: str, files: dict[str, str], message: str) -> None:
        # The contents API commits one file per call — fine at autofix scale
        # (a handful of files), and it spares the blob/tree/commit plumbing.
        for path, content in files.items():
            payload: dict[str, Any] = {
                "message": f"{message} ({path})",
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            # Updating an existing file requires its current blob sha; a new
            # file must omit it. 404 here is information, not an error.
            probe = await self.http.get(
                f"{self._api}/contents/{path}?ref={branch}", headers=self._headers()
            )
            if probe.status_code == 200:
                payload["sha"] = probe.json()["sha"]
            await self._request("PUT", f"{self._api}/contents/{path}", json=payload)

    async def open_pull_request(
        self, branch: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"{self._api}/pulls",
            json={"title": title, "body": body, "head": branch, "base": base},
        )
        data = response.json()
        return {"url": data["html_url"], "number": data["number"]}


class GitLabClient(RepoClient):
    """gitlab.com or self-hosted. `repo` is the project path (group/project)."""

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    @property
    def _api(self) -> str:
        from urllib.parse import quote

        return f"{self.base_url}/api/v4/projects/{quote(self.repo, safe='')}"

    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"{self._api}/issues",
            json={"title": title, "description": body, "labels": ",".join(labels)},
        )
        data = response.json()
        return {"url": data["web_url"], "number": data["iid"]}

    async def branch_sha(self, branch: str) -> str:
        response = await self._request("GET", f"{self._api}/repository/branches/{branch}")
        return response.json()["commit"]["id"]

    async def list_paths(self, ref: str) -> list[str]:
        response = await self._request(
            "GET", f"{self._api}/repository/tree?recursive=true&per_page=100&ref={ref}"
        )
        return [item["path"] for item in response.json() if item["type"] == "blob"]

    async def read_file(self, path: str, ref: str) -> str:
        from urllib.parse import quote

        response = await self._request(
            "GET", f"{self._api}/repository/files/{quote(path, safe='')}?ref={ref}"
        )
        return base64.b64decode(response.json()["content"]).decode("utf-8", errors="replace")

    async def create_branch(self, name: str, from_sha: str) -> None:
        await self._request(
            "POST", f"{self._api}/repository/branches", params={"branch": name, "ref": from_sha}
        )

    async def commit_files(self, branch: str, files: dict[str, str], message: str) -> None:
        # GitLab's commits API takes every file in one commit — the shape the
        # GitHub path only approximates.
        actions = [
            {"action": "update", "file_path": path, "content": content}
            for path, content in files.items()
        ]
        response = await self.http.post(
            f"{self._api}/repository/commits",
            headers=self._headers(),
            json={"branch": branch, "commit_message": message, "actions": actions},
        )
        if response.status_code == 400 and "does not exist" in response.text:
            # New files need action=create; retry the ones GitLab rejected.
            for action in actions:
                action["action"] = "create"
            response = await self.http.post(
                f"{self._api}/repository/commits",
                headers=self._headers(),
                json={"branch": branch, "commit_message": message, "actions": actions},
            )
        if response.status_code >= 400:
            raise NodeError(f"GitLab commit failed → {response.status_code}: {response.text[:300]}")

    async def open_pull_request(
        self, branch: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"{self._api}/merge_requests",
            json={
                "source_branch": branch,
                "target_branch": base,
                "title": title,
                "description": body,
            },
        )
        data = response.json()
        return {"url": data["web_url"], "number": data["iid"]}


DEFAULT_BASE = {"github": "https://api.github.com", "gitlab": "https://gitlab.com"}


async def make_client(
    ctx: NodeContext, *, git_provider: str, git_credential_id: str, repo: str, base_url: str = ""
) -> RepoClient:
    credential = await ctx.resolve_credential(git_credential_id)
    if credential is None:
        raise NodeError(
            f"Git credential {git_credential_id!r} was not found in this workspace.",
            retryable=False,
        )
    if credential.provider != git_provider:
        raise NodeError(
            f"This credential is for {credential.provider!r}, not {git_provider!r}.",
            retryable=False,
        )
    resolved_base = base_url or credential.base_url or DEFAULT_BASE[git_provider]
    cls = GitHubClient if git_provider == "github" else GitLabClient
    return cls(ctx.http, token=credential.api_key, base_url=resolved_base, repo=repo)


# ---------------------------------------------------------------------------
# git.ticket — raise the issue
# ---------------------------------------------------------------------------


class TicketConfig(BaseModel):
    model_config = {"extra": "forbid"}

    git_provider: VcsProvider = "github"
    git_credential_id: str = Field(default="", description="A saved GitHub/GitLab credential.")
    repo: str = Field(min_length=1, max_length=200, description="owner/name or group/project.")
    title: str = Field(min_length=1, max_length=400, description="Supports {{ references }}.")
    body: str = Field(default="", max_length=20000, description="Supports {{ references }}.")
    labels: list[str] = Field(default_factory=list, max_length=10)


class TicketNode(Node):
    type = "git.ticket"
    label = "Raise Ticket"
    description = "Open a GitHub/GitLab issue from run data — the paper trail before the fix."
    tier = 2
    category = "devops"
    config_model = TicketConfig
    output_paths = ("url", "number")

    max_attempts = 2
    retry_backoff_seconds = 2.0
    timeout_seconds = 60.0

    async def run(self, config: TicketConfig, ctx: NodeContext) -> NodeResult:
        template = ctx.template_context()
        title = str(render_value(config.title, template))
        body = str(render_value(config.body, template))

        client = await make_client(
            ctx,
            git_provider=config.git_provider,
            git_credential_id=config.git_credential_id,
            repo=config.repo,
        )
        await ctx.progress(f"Opening issue on {config.git_provider}:{config.repo}")
        issue = await client.create_issue(title, body, config.labels)

        await ctx.step(
            "ticket.created",
            {"provider": config.git_provider, "repo": config.repo, **issue},
        )
        return NodeResult(output=issue)


# ---------------------------------------------------------------------------
# git.autofix — the agent repairs the code and opens the PR/MR
# ---------------------------------------------------------------------------


class AutofixConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # -- where the code lives ------------------------------------------------
    git_provider: VcsProvider = "github"
    git_credential_id: str = Field(default="", description="A saved GitHub/GitLab credential.")
    repo: str = Field(min_length=1, max_length=200, description="owner/name or group/project.")
    base_branch: str = Field(default="main", max_length=200)
    branch_prefix: str = Field(default="basivo/autofix", max_length=100)

    # -- what is broken --------------------------------------------------------
    problem: str = Field(
        min_length=1,
        max_length=20000,
        description="What to fix. Supports {{ references }} — e.g. {{ input.error }}.",
    )
    instructions: str = Field(
        default="",
        max_length=10000,
        description="House rules for the fix. Supports {{ references }}.",
    )

    # -- which model does the fixing -----------------------------------------
    provider: str = Field(default="anthropic", max_length=48)
    model: str = Field(default="claude-sonnet-5", max_length=160)
    credential_id: str = Field(default="", description="A saved model credential.")

    # -- limits -----------------------------------------------------------------
    max_iterations: int = Field(default=12, ge=2, le=40)
    max_tool_calls: int = Field(default=40, ge=2, le=200)
    cost_limit_usd: float | None = Field(default=None, ge=0)
    max_files: int = Field(default=10, ge=1, le=50, description="Refuse fixes touching more.")

    @model_validator(mode="after")
    def _repo_shape(self) -> AutofixConfig:
        if "/" not in self.repo:
            raise ValueError("repo must be owner/name (GitHub) or group/project (GitLab).")
        return self


SYSTEM_PROMPT = """You are an automated repair agent operating on a real repository.

Work method, strictly:
1. list_files, then read_file the files that plausibly relate to the problem.
2. Decide the minimal fix. Do not refactor, reformat, or improve unrelated code.
3. write_file each changed file with its COMPLETE new content (writes are staged;
   nothing is pushed until you finish).
4. When the fix is staged, reply with a short summary: what was wrong, what you
   changed, and how to verify it. This summary becomes the pull request body.

If you cannot find the cause, change nothing and say exactly what you looked at
and what you would need — an honest miss beats a speculative edit."""


class AutofixNode(Node):
    type = "git.autofix"
    label = "Auto-fix & PR"
    description = (
        "An agent reads the repo, stages a minimal fix, and opens a pull/merge "
        "request for humans to review. It never merges."
    )
    tier = 2
    category = "devops"
    config_model = AutofixConfig
    output_paths = (
        "pr_url",
        "pr_number",
        "branch",
        "files_changed",
        "summary",
        "usage.input_tokens",
        "usage.output_tokens",
        "usage.cost_usd",
    )

    #: One attempt: a retried half-fix means duplicate branches and PRs.
    max_attempts = 1
    timeout_seconds = 840.0

    async def run(self, config: AutofixConfig, ctx: NodeContext) -> NodeResult:
        from basivo_orch.flows.nodes.agent import build_llm_model

        template = ctx.template_context()
        problem = str(render_value(config.problem, template))
        instructions = (
            str(render_value(config.instructions, template)) if config.instructions else ""
        )

        client = await make_client(
            ctx,
            git_provider=config.git_provider,
            git_credential_id=config.git_credential_id,
            repo=config.repo,
        )
        base_sha = await client.branch_sha(config.base_branch)

        staged: dict[str, str] = {}

        async def list_files() -> list[str]:
            """List every file path in the repository."""
            paths = await client.list_paths(config.base_branch)
            await ctx.step("fix.listed", {"files": len(paths)})
            return paths

        async def read_file(path: str) -> str:
            """Read one file's full content."""
            if path in staged:
                return staged[path]
            content = await client.read_file(path, config.base_branch)
            await ctx.step("fix.read", {"path": path, "bytes": len(content)})
            return content

        async def write_file(path: str, content: str) -> str:
            """Stage a file's complete new content. Nothing is pushed yet."""
            if path not in staged and len(staged) >= config.max_files:
                return (
                    f"Refused: this fix already touches {config.max_files} files, "
                    "the configured limit. Keep the change smaller."
                )
            staged[path] = content
            await ctx.step("fix.staged", {"path": path, "bytes": len(content)})
            return f"Staged {path} ({len(content)} bytes)."

        model = await build_llm_model(
            ctx, provider=config.provider, model=config.model, credential_id=config.credential_id
        )
        agent: Agent[None, str] = Agent(
            model,
            instructions=SYSTEM_PROMPT
            + (f"\n\nHouse rules:\n{instructions}" if instructions else ""),
            tools=[Tool(list_files), Tool(read_file), Tool(write_file)],
        )

        await ctx.step(
            "fix.started",
            {
                "repo": config.repo,
                "base_branch": config.base_branch,
                "model": config.model,
                "problem_preview": problem[:300],
            },
        )
        await ctx.progress(f"Repair agent reading {config.repo}")

        try:
            result = await agent.run(
                f"Problem to fix:\n\n{problem}",
                usage_limits=UsageLimits(
                    request_limit=config.max_iterations,
                    tool_calls_limit=config.max_tool_calls,
                    cost_limit=config.cost_limit_usd,
                ),
            )
        except UsageLimitExceeded as exc:
            raise NodeError(f"The repair agent hit its limits before finishing: {exc}") from exc
        except (ModelHTTPError, UnexpectedModelBehavior) as exc:
            raise NodeError(f"The model provider returned an error: {exc}", retryable=True) from exc

        summary = result.output if isinstance(result.output, str) else json.dumps(result.output)
        usage = result.usage
        cost = float(usage.cost) if usage.cost is not None else 0.0

        if not staged:
            # An honest miss, surfaced as a failure the flow can branch on —
            # not a green run with an empty PR.
            raise NodeError(f"The agent changed no files. Its report:\n{summary[:1200]}")

        branch = f"{config.branch_prefix}-{str(ctx.run_id)[:8]}"
        await ctx.progress(f"Committing {len(staged)} file(s) to {branch}")
        await client.create_branch(branch, base_sha)
        await client.commit_files(
            branch, staged, f"Autofix: {problem.splitlines()[0][:60]} [run {str(ctx.run_id)[:8]}]"
        )
        await ctx.step("fix.committed", {"branch": branch, "files": sorted(staged)})

        pr = await client.open_pull_request(
            branch,
            config.base_branch,
            title=f"Autofix: {problem.splitlines()[0][:80]}",
            body=(
                f"{summary}\n\n---\n*Opened automatically by a Basivo autofix run "
                f"`{str(ctx.run_id)[:8]}`. Review before merging — this bot never merges.*"
            ),
        )
        await ctx.step("pr.opened", {**pr, "branch": branch})

        return NodeResult(
            output={
                "pr_url": pr["url"],
                "pr_number": pr["number"],
                "branch": branch,
                "files_changed": sorted(staged),
                "summary": summary,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cost_usd": cost,
                },
            },
            metrics={
                "tokens_in": usage.input_tokens,
                "tokens_out": usage.output_tokens,
                "cost_usd": cost,
            },
        )
