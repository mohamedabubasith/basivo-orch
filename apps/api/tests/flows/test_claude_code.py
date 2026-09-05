"""The Claude Code runner, against a fake `claude` binary.

The real CLI is not on CI machines and would cost money if it were. What these
prove is the contract around it: which flags it is launched with, that the key
travels only in the environment, that its JSON is read correctly, and that the
ways it can fail become clear errors rather than half-pushed fixes.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from basivo_orch.flows.nodes import claude_code
from basivo_orch.flows.nodes.base import NodeError

KEY = "sk-ant-test-0123456789abcdef"


def fake_claude(tmp_path: Path, body: str) -> Path:
    """A stand-in CLI. `body` is Python that runs with `argv`, `prompt`, `cwd`."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "prompt = sys.stdin.read()\n"
        "cwd = os.getcwd()\n"
        f"{body}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.fixture
def use_fake(monkeypatch, tmp_path):
    def install(body: str) -> Path:
        script = fake_claude(tmp_path, body)
        monkeypatch.setenv("BASIVO_CLAUDE_CODE_BIN", str(script))
        return script

    return install


async def test_it_is_launched_headless_with_file_tools_only(use_fake, tmp_path):
    """The flags are the safety story: no Bash, no web, no host config."""
    use_fake(
        "print(json.dumps({'result': json.dumps(argv), 'total_cost_usd': 0.01, "
        "'num_turns': 2, 'session_id': 's1', 'usage': {'input_tokens': 10, 'output_tokens': 5}}))"
    )
    result = await claude_code.run_claude_code(
        cwd=tmp_path,
        prompt="fix it",
        system_prompt="rules",
        api_key=KEY,
        model="claude-sonnet-5",
        max_turns=7,
        max_budget_usd=1.5,
    )
    argv = json.loads(result.text)

    assert argv[:2] == ["-p", "--bare"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert "Bash" in argv[argv.index("--disallowedTools") + 1]
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.50"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert KEY not in " ".join(argv), "the key must never be on the command line"

    assert result.cost_usd == 0.01
    assert result.turns == 2
    assert result.input_tokens == 10 and result.output_tokens == 5
    assert result.session_id == "s1"


async def test_the_key_arrives_in_the_environment_and_the_prompt_on_stdin(use_fake, tmp_path):
    use_fake(
        "print(json.dumps({'result': os.environ.get('ANTHROPIC_API_KEY','') + '|' + prompt "
        "+ '|' + os.environ.get('HOME','') + '|' + os.environ.get('DISABLE_TELEMETRY','')}))"
    )
    result = await claude_code.run_claude_code(
        cwd=tmp_path, prompt="the whole bug report", system_prompt="", api_key=KEY
    )
    key, prompt, home, telemetry = result.text.split("|")
    assert key == "[redacted]", "the key was in the output, so redaction ran on it"
    assert prompt == "the whole bug report"
    assert home.startswith(os.path.join(os.sep, "")) and "basivo-claude-home-" in home
    assert telemetry == "1"


async def test_a_subscription_token_travels_in_its_own_variable(use_fake, tmp_path):
    """`claude setup-token` output is not an API key. Handed to Claude Code as
    one it would be rejected; in CLAUDE_CODE_OAUTH_TOKEN it signs in."""
    use_fake(
        "print(json.dumps({'result': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN','-') + '|' "
        "+ os.environ.get('ANTHROPIC_API_KEY','-')}))"
    )
    token = "sk-ant-oat01-subscription-token-0123456789"
    result = await claude_code.run_claude_code(
        cwd=tmp_path, prompt="x", system_prompt="", api_key=token
    )
    oauth, api = result.text.split("|")
    assert oauth == "[redacted]" and api == "-"
    assert claude_code.is_subscription_token(token)
    assert not claude_code.is_subscription_token("sk-ant-api03-real-key")


async def test_a_subscription_token_runs_without_bare_but_still_isolated(use_fake, tmp_path):
    """`--bare` only accepts an API key, so the token path drops it and states
    the same isolation flag by flag: no repo-level settings, no MCP, no
    session files. Found by a real run that failed with "Not logged in"."""
    use_fake("print(json.dumps({'result': json.dumps(argv)}))")
    result = await claude_code.run_claude_code(
        cwd=tmp_path, prompt="x", system_prompt="", api_key="sk-ant-oat01-token-0123456789"
    )
    argv = json.loads(result.text)
    assert "--bare" not in argv
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv


async def test_the_key_never_appears_in_an_error(use_fake, tmp_path):
    use_fake("sys.stderr.write('auth failed for ' + os.environ['ANTHROPIC_API_KEY']); sys.exit(1)")
    with pytest.raises(NodeError) as raised:
        await claude_code.run_claude_code(cwd=tmp_path, prompt="x", system_prompt="", api_key=KEY)
    assert KEY not in str(raised.value)
    assert "[redacted]" in str(raised.value)
    assert "status 1" in str(raised.value)


async def test_a_partial_run_is_a_failure_not_a_half_fix(use_fake, tmp_path):
    """Exit 2 with JSON: the cost ceiling stopped it mid-way. Edits may be on
    disk, and pushing them would be a branch that does half of something."""
    use_fake("print(json.dumps({'result': 'ran out of budget'})); sys.exit(2)")
    with pytest.raises(NodeError, match="stopped early"):
        await claude_code.run_claude_code(cwd=tmp_path, prompt="x", system_prompt="", api_key=KEY)


async def test_an_error_flag_in_the_json_is_an_error(use_fake, tmp_path):
    use_fake("print(json.dumps({'result': 'no', 'is_error': True, 'subtype': 'error_max_turns'}))")
    with pytest.raises(NodeError, match="error_max_turns"):
        await claude_code.run_claude_code(cwd=tmp_path, prompt="x", system_prompt="", api_key=KEY)


async def test_a_hung_process_is_killed(use_fake, tmp_path):
    use_fake("import time; time.sleep(30)")
    with pytest.raises(NodeError, match="did not finish"):
        await claude_code.run_claude_code(
            cwd=tmp_path, prompt="x", system_prompt="", api_key=KEY, timeout_seconds=0.5
        )


async def test_a_warning_line_before_the_json_is_tolerated(use_fake, tmp_path):
    use_fake("print('(node) warning: something'); print(json.dumps({'result': 'ok'}))")
    result = await claude_code.run_claude_code(
        cwd=tmp_path, prompt="x", system_prompt="", api_key=KEY
    )
    assert result.text == "ok"


async def test_missing_binary_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BASIVO_CLAUDE_CODE_BIN", "")
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: None)
    with pytest.raises(NodeError, match="not installed"):
        await claude_code.run_claude_code(cwd=tmp_path, prompt="x", system_prompt="", api_key=KEY)


# ---------------------------------------------------------------------------
# The working copy
# ---------------------------------------------------------------------------


def targz(files: dict[str, bytes], top: str = "acme-api-abc123") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_extract_strips_the_hosts_top_level_directory(tmp_path):
    root = claude_code.extract_archive(targz({"calc.py": b"x = 1\n", "a/b.txt": b"hi"}), tmp_path)
    assert (root / "calc.py").read_bytes() == b"x = 1\n"
    assert (root / "a" / "b.txt").read_bytes() == b"hi"


def test_a_hostile_archive_cannot_write_outside_the_directory(tmp_path):
    """`filter="data"` is what makes `../` refuse rather than land in HOME."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("top/../../escaped.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    with pytest.raises(NodeError, match="refused"):
        claude_code.extract_archive(buffer.getvalue(), tmp_path)
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_changed_files_sees_edits_additions_and_deletions(tmp_path):
    root = claude_code.extract_archive(
        targz({"keep.py": b"same", "edit.py": b"old", "gone.py": b"bye"}), tmp_path
    )
    before = claude_code.snapshot(root)
    (root / "edit.py").write_bytes(b"new")
    (root / "gone.py").unlink()
    (root / "added.py").write_bytes(b"fresh")
    # The report folder is where screenshots go for the agent to read; it must
    # never look like part of the fix.
    (root / claude_code.REPORT_DIR).mkdir()
    (root / claude_code.REPORT_DIR / "image-1.png").write_bytes(b"png")

    changes = claude_code.changed_files(before, claude_code.snapshot(root))

    assert changes == {"edit.py": b"new", "gone.py": None, "added.py": b"fresh"}


async def test_mcp_servers_travel_in_a_private_file_not_on_the_command_line(use_fake, tmp_path):
    """The config can carry a bearer token in its headers, and the command line
    is readable by every process on the worker; so it goes through a file in
    the throwaway HOME, and the agent is allowed to call the servers."""
    use_fake(
        "cfg = next(a for a in argv if a.endswith('mcp.json')); "
        "print(json.dumps({'result': json.dumps({'argv': argv, 'cfg': open(cfg).read()})}))"
    )
    result = await claude_code.run_claude_code(
        cwd=tmp_path,
        prompt="x",
        system_prompt="",
        api_key="sk-ant-api-key",
        mcp_config={
            "mcpServers": {
                "docs": {
                    "type": "http",
                    "url": "https://mcp.test/mcp",
                    "headers": {"Authorization": "Bearer tok-secret"},
                }
            }
        },
        extra_allowed_tools=["mcp__docs"],
    )
    payload = json.loads(result.text)
    argv = payload["argv"]
    assert "--mcp-config" in argv and argv.count("--strict-mcp-config") == 1
    assert "tok-secret" not in " ".join(argv), "the token is in the file, not the process table"
    assert json.loads(payload["cfg"])["mcpServers"]["docs"]["headers"]["Authorization"] == (
        "Bearer tok-secret"
    )
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "mcp__docs" in allowed and "Read" in allowed
