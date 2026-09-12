"""The coding engines: which one runs, and how each is driven.

Every test here uses a fake CLI written to `tmp_path` and pointed at by the
engine's `BASIVO_*_BIN` variable. That is the only honest way to test this
module: the real binaries cost money, need network, and are the thing under
test only in the sense that we have to get their arguments right. So the fakes
record what they were given and answer in the real output format, and the
assertions are about the flags and the parsing.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from contextlib import suppress
from pathlib import Path

import pytest

from basivo_orch.flows.nodes import engines
from basivo_orch.flows.nodes.base import NodeError

pytestmark = pytest.mark.anyio


def _fake(tmp_path: Path, name: str, body: str) -> Path:
    """A stand-in CLI that dumps its argv and environment, then answers."""
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        # The throwaway HOME is gone by the time the test looks, so whatever
        # the engine wrote into it is copied out here instead.
        "cfg = os.environ.get('OPENCODE_CONFIG', '')\n"
        "home = os.environ.get('HOME', '')\n"
        "db = os.path.join(home, 'data', 'opencode', 'opencode.db')\n"
        "json.dump({'db': open(db).read() if os.path.exists(db) else None,"
        " 'config_is_link': os.path.islink(os.path.join(home, 'config')),"
        " 'config_has_modules': os.path.isdir(os.path.join(home, 'config', 'opencode', 'node_modules'))},"
        f" open({str(tmp_path / f'{name}-home.json')!r}, 'w'))\n"
        "json.dump({'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd(),"
        " 'config': json.load(open(cfg)) if cfg and os.path.exists(cfg) else None},"
        f" open({str(tmp_path / f'{name}-call.json')!r}, 'w'))\n" + body + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _call(tmp_path: Path, name: str) -> dict:
    return json.loads((tmp_path / f"{name}-call.json").read_text())


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------


def test_a_workspace_with_no_credential_gets_the_free_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", "")))
    engine, reason = engines.choose("auto", provider="anthropic", has_credential=False)
    assert engine is not None and engine.name == "opencode"
    assert "free" in reason
    # Never the model, never the supplier: that is a deployment detail and it
    # will change.
    assert "pickle" not in engine.label.lower() and "zen" not in reason.lower()


def test_without_the_free_agent_the_old_behaviour_stands(monkeypatch):
    """A worker that has no coding agent installed still runs the builtin loop.

    Falling back matters more than being clever: a self-hosted deployment with
    provider keys in its environment was working before the free agent existed
    and must keep working after.
    """
    engine, reason = engines.choose("auto", provider="openai", has_credential=False)
    assert engine is None and reason == "openai cannot drive Claude Code"


def test_a_credential_keeps_the_engine_it_pays_for(monkeypatch, tmp_path):
    monkeypatch.setenv("BASIVO_CLAUDE_CODE_BIN", str(_fake(tmp_path, "claude", "")))
    monkeypatch.setenv("BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", "")))
    engine, reason = engines.choose("auto", provider="anthropic", has_credential=True)
    assert engine is not None and engine.name == "claude_code"
    assert reason == "Anthropic credential, Claude Code installed"


def test_codex_is_chosen_never_inherited(monkeypatch, tmp_path):
    """An OpenAI credential does not silently move a flow onto Codex.

    A flow that has been opening acceptable pull requests through the builtin
    loop must not change engine because the worker image was rebuilt. Codex is
    there when someone picks it.
    """
    monkeypatch.setenv("BASIVO_CODEX_BIN", str(_fake(tmp_path, "codex", "")))
    engine, _ = engines.choose("auto", provider="openai", has_credential=True)
    assert engine is None

    engine, reason = engines.choose("codex", provider="openai", has_credential=True)
    assert engine is not None and engine.name == "codex" and reason == "chosen on the node"


def test_a_paid_engine_without_a_credential_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("BASIVO_CODEX_BIN", str(_fake(tmp_path, "codex", "")))
    with pytest.raises(NodeError, match="needs a credential"):
        engines.choose("codex", provider="openai", has_credential=False)


def test_an_engine_that_is_not_installed_says_so():
    with pytest.raises(NodeError, match="not installed"):
        engines.choose("opencode", provider="anthropic", has_credential=False)


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------

#: What `opencode run --format json` actually prints: one JSON object a line,
#: the reply arriving as text parts.
_OPENCODE_OUTPUT = (
    "print(json.dumps({'type': 'step_start', 'part': {'type': 'step-start',"
    " 'sessionID': 'ses_1'}}))\n"
    # Talking to itself between tools: never what the person is shown.
    "print(json.dumps({'type': 'text', 'part': {'type': 'text',"
    " 'text': 'Let me check the types first.'}}))\n"
    "print(json.dumps({'type': 'tool_use', 'part': {'type': 'tool', 'tool': 'write',"
    " 'state': {'status': 'completed'}}}))\n"
    "print(json.dumps({'type': 'text', 'part': {'type': 'text',"
    " 'text': 'Fixed the operator.'}}))\n"
)


async def test_the_free_agent_runs_with_no_credential_and_no_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", _OPENCODE_OUTPUT)))
    work = tmp_path / "work"
    work.mkdir()

    result = await engines.ENGINES["opencode"].run(
        cwd=work,
        prompt="Fix add().",
        system_prompt="House rules here.",
        timeout_seconds=30,
    )

    assert result.text == "Fixed the operator."
    assert result.turns == 1 and result.session_id == "ses_1"

    call = _call(tmp_path, "opencode")
    assert "--pure" in call["argv"] and "--format" in call["argv"]
    assert "Fix add()." in call["argv"]
    # No credential was asked for and none was passed.
    assert "OPENCODE_API_KEY" not in call["env"]

    config = call["config"]
    tools = config["agent"]["build"]["tools"]
    assert tools["bash"] is False and tools["webfetch"] is False
    assert config["agent"]["build"]["prompt"] == "House rules here."
    # A permission block would make a headless run hang on an approval nobody
    # can give. Removing the tool is the only thing that works.
    assert "permission" not in config


async def test_the_free_agent_carries_our_documentation_server(monkeypatch, tmp_path):
    monkeypatch.setenv("BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", _OPENCODE_OUTPUT)))
    work = tmp_path / "work"
    work.mkdir()

    await engines.ENGINES["opencode"].run(
        cwd=work,
        prompt="x",
        system_prompt="",
        timeout_seconds=30,
        mcp_servers={
            "basivo_docs": {"type": "stdio", "command": "/usr/bin/python3", "args": ["-m", "docs"]}
        },
    )
    config = _call(tmp_path, "opencode")["config"]
    assert config["mcp"]["basivo_docs"] == {
        "type": "local",
        "command": ["/usr/bin/python3", "-m", "docs"],
        "environment": {},
        "enabled": True,
    }


async def test_an_engine_that_cannot_narrow_a_server_refuses_to_widen_it(monkeypatch, tmp_path):
    """A server restricted to two tools must not become a server with all of them."""
    monkeypatch.setenv("BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", _OPENCODE_OUTPUT)))
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(NodeError, match="cannot limit an MCP server"):
        await engines.ENGINES["opencode"].run(
            cwd=work,
            prompt="x",
            system_prompt="",
            timeout_seconds=30,
            mcp_servers={"jira": {"url": "https://mcp.test/mcp"}},
            allowed_mcp_tools=["mcp__jira__create_issue"],
        )


async def test_a_hung_agent_is_killed_and_named(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", "import time; time.sleep(30)"))
    )
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(NodeError, match="OpenCode did not finish within 1s"):
        await engines.ENGINES["opencode"].run(
            cwd=work, prompt="x", system_prompt="", timeout_seconds=1
        )


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


async def test_codex_is_sandboxed_takes_the_task_on_stdin_and_hides_the_key(monkeypatch, tmp_path):
    body = (
        "task = sys.stdin.read()\n"
        f"open({str(tmp_path / 'codex-task.txt')!r}, 'w').write(task)\n"
        "last = sys.argv[sys.argv.index('-o') + 1]\n"
        "open(last, 'w').write('Fixed it.')\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution'}}))\n"
    )
    monkeypatch.setenv("BASIVO_CODEX_BIN", str(_fake(tmp_path, "codex", body)))
    work = tmp_path / "work"
    work.mkdir()

    result = await engines.ENGINES["codex"].run(
        cwd=work,
        prompt="Fix add().",
        system_prompt="House rules here.",
        api_key="sk-secret-value",
        model="gpt-5",
        timeout_seconds=30,
    )

    assert result.text == "Fixed it."
    call = _call(tmp_path, "codex")
    argv = call["argv"]
    assert argv[0] == "exec" and "-" in argv
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=false" in argv
    assert "--ignore-user-config" in argv
    # The key travels in the environment, never in the process table.
    assert "sk-secret-value" not in " ".join(argv)
    assert call["env"]["OPENAI_API_KEY"] == "sk-secret-value"
    assert call["env"]["CODEX_HOME"] == call["env"]["HOME"]

    task = (tmp_path / "codex-task.txt").read_text()
    assert task.startswith("House rules here.") and "Fix add()." in task


async def test_codex_reports_what_it_said_when_it_fails(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "BASIVO_CODEX_BIN",
        str(
            _fake(
                tmp_path, "codex", "sys.stdin.read()\nprint('boom', file=sys.stderr)\nsys.exit(3)"
            )
        ),
    )
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(NodeError, match="exited with status 3"):
        await engines.ENGINES["codex"].run(
            cwd=work, prompt="x", system_prompt="", api_key="k", timeout_seconds=30
        )


async def test_a_warmed_home_is_copied_whole_so_nothing_is_shared_between_runs(
    monkeypatch, tmp_path
):
    """Both halves of the warmed home are copied, never shared.

    Sessions are a tenant's. The package directory would be too, in the wrong
    way: a shared writable copy lets one tenant's agent drop a plugin that runs
    inside the next tenant's session. Fifty megabytes a turn is the price.
    """
    monkeypatch.setenv("BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", _OPENCODE_OUTPUT)))
    template = tmp_path / "warm"
    (template / "data" / "opencode").mkdir(parents=True)
    (template / "data" / "opencode" / "opencode.db").write_text("migrated")
    (template / "config" / "opencode" / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(engines, "OPENCODE_HOME_TEMPLATE", str(template))

    work = tmp_path / "work"
    work.mkdir()
    await engines.ENGINES["opencode"].run(
        cwd=work, prompt="x", system_prompt="", timeout_seconds=30
    )

    home = Path(_call(tmp_path, "opencode")["env"]["HOME"])
    seen = json.loads((tmp_path / "opencode-home.json").read_text())
    assert seen["db"] == "migrated"
    assert seen["config_is_link"] is False and seen["config_has_modules"] is True
    gone = not await asyncio.to_thread(os.path.exists, home)
    assert gone, "the throwaway home is deleted with the run"


async def test_an_agent_that_goes_quiet_is_killed_long_before_the_ceiling(monkeypatch, tmp_path):
    """The failure that hides from every overall timeout: a provider holding
    the socket open and sending nothing. Streaming agents print an event per
    token, so silence is the signal, and it is acted on in seconds rather than
    at a ten minute ceiling somebody is staring at."""
    monkeypatch.setenv(
        "BASIVO_OPENCODE_BIN",
        str(
            _fake(
                tmp_path,
                "opencode",
                "import time\n"
                "print(json.dumps({'type': 'step_start',"
                " 'part': {'type': 'step-start'}}), flush=True)\n"
                "time.sleep(30)\n",
            )
        ),
    )
    monkeypatch.setattr(engines, "STALL_SECONDS", 1.0)
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(NodeError, match="stopped responding: nothing for 1 seconds"):
        await engines.ENGINES["opencode"].run(
            cwd=work, prompt="x", system_prompt="", timeout_seconds=60
        )


async def test_every_cli_agent_runs_inside_the_jail(monkeypatch, tmp_path):
    """The wall, wired. An engine that stopped wrapping its argv would let a
    prompt read /proc/1/environ, and nothing else in the suite would notice."""
    monkeypatch.setenv("BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", _OPENCODE_OUTPUT)))
    monkeypatch.setenv("BASIVO_CODEX_BIN", str(_fake(tmp_path, "codex", "sys.stdin.read()")))
    monkeypatch.setenv("BASIVO_CLAUDE_CODE_BIN", str(_fake(tmp_path, "claude", "sys.stdin.read()")))

    from basivo_orch.flows.nodes import jail

    wrapped: list[list[str]] = []

    def fake_wrap(argv, *, workspace, home, read_only=()):
        wrapped.append(list(argv))
        # The workspace and the agent's home are what may be written, and the
        # engine has to hand over both or the jail cannot let the agent work.
        assert workspace.exists() and home.exists()
        return ["/usr/bin/env", *argv]

    monkeypatch.setattr(jail, "wrap", fake_wrap)
    monkeypatch.setattr(jail, "MODE", "auto")

    work = tmp_path / "work"
    work.mkdir()
    for name in ("opencode", "codex", "claude_code"):
        wrapped.clear()
        with suppress(NodeError):
            await engines.ENGINES[name].run(
                cwd=work, prompt="x", system_prompt="", api_key="k", timeout_seconds=30
            )
        assert wrapped, f"{name} ran its binary without the jail"
        assert name.split("_")[0] in wrapped[0][0]


async def test_the_first_turn_warms_what_the_image_could_not(monkeypatch, tmp_path):
    """The image warms OpenCode's packages at build time when the model
    answers. When it could not, a build must still ship, so the first turn
    that installs them keeps a copy for every turn after it."""
    body = _OPENCODE_OUTPUT + (
        "home = os.environ['HOME']\n"
        "os.makedirs(os.path.join(home, 'config', 'opencode', 'node_modules'), exist_ok=True)\n"
        "open(os.path.join(home, 'config', 'opencode', 'node_modules', 'marker'), 'w').write('x')\n"
    )
    monkeypatch.setenv("BASIVO_OPENCODE_BIN", str(_fake(tmp_path, "opencode", body)))
    cache = tmp_path / "warm"
    monkeypatch.setattr(engines, "OPENCODE_HOME_TEMPLATE", "")
    monkeypatch.setattr(engines, "OPENCODE_WARM_CACHE", str(cache))

    work = tmp_path / "work"
    work.mkdir()
    await engines.ENGINES["opencode"].run(
        cwd=work, prompt="x", system_prompt="", timeout_seconds=30
    )

    assert (cache / "config" / "opencode" / "node_modules" / "marker").exists()

    # And the next run starts from it rather than installing again.
    home = tmp_path / "next"
    home.mkdir()
    await asyncio.to_thread(engines._write_opencode_home, home, {"agent": {}})
    assert (home / "config" / "opencode" / "node_modules" / "marker").exists()
