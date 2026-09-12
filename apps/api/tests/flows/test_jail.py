"""The jail around a coding agent: what it may see, and that it may see nothing else.

The argv construction is tested on every platform. The confinement itself is
tested for real wherever a jail exists: on macOS with `sandbox-exec`, on Linux
with `bwrap` where user namespaces are allowed. A test that only checked the
flags would be a test of our typing, not of the wall.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

import pytest

from basivo_orch.flows.nodes import jail
from basivo_orch.flows.nodes.base import NodeError


@pytest.fixture(autouse=True)
def _fresh_probe():
    jail.tool.cache_clear()
    yield
    # Some tests replace `tool` with a plain function for the duration.
    if hasattr(jail.tool, "cache_clear"):
        jail.tool.cache_clear()


def test_required_mode_refuses_to_run_an_agent_without_a_jail(monkeypatch, tmp_path):
    monkeypatch.setattr(jail, "MODE", "required")
    monkeypatch.setattr(jail, "tool", lambda: None)
    with pytest.raises(NodeError, match="only inside an OS jail"):
        jail.wrap(["agent"], workspace=tmp_path, home=tmp_path)


def test_the_refusal_names_the_reason_rather_than_the_documentation(monkeypatch, tmp_path):
    """An operator reading this in a run log needs the kernel's own words. The
    first version said only that no jail was available, which is the one fact
    they already knew."""
    monkeypatch.setattr(jail, "MODE", "required")
    monkeypatch.setattr(jail, "tool", lambda: None)
    monkeypatch.setattr(
        jail, "refusal", lambda: "bwrap exited 1: setting up uid map: Permission denied"
    )
    with pytest.raises(NodeError, match="setting up uid map"):
        jail.wrap(["agent"], workspace=tmp_path, home=tmp_path)


def test_a_missing_binary_and_a_refused_one_read_differently(monkeypatch, tmp_path):
    monkeypatch.setattr(jail, "MODE", "required")
    monkeypatch.setattr(jail, "_find", lambda name: None)
    jail.tool.cache_clear()
    assert jail.tool() is None
    assert "not installed" in jail.refusal()


def test_auto_mode_runs_unjailed_and_says_so_once(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(jail, "MODE", "auto")
    monkeypatch.setattr(jail, "tool", lambda: None)
    monkeypatch.setattr(jail, "_warned", False)
    assert jail.wrap(["agent", "--x"], workspace=tmp_path, home=tmp_path) == ["agent", "--x"]
    assert jail._warned is True


def test_the_bwrap_command_binds_only_what_the_agent_needs(monkeypatch, tmp_path):
    """The workspace and HOME read-write, the system read-only, a private
    /tmp and /proc, nothing from any home directory, and the agent dies with
    the worker."""
    monkeypatch.setattr(jail, "tool", lambda: "bwrap")
    monkeypatch.setattr(jail, "_find", lambda name: "/usr/bin/bwrap")
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    modules = tmp_path / "baked_modules"
    modules.mkdir()
    (work / "node_modules").symlink_to(modules)

    command = jail._bwrap(
        ["opencode", "run"],
        workspace=work,
        home=home,
        read_only=[*jail._linked_targets(work)],
    )

    assert command[0] == "/usr/bin/bwrap"
    for flag in ("--unshare-all", "--share-net", "--die-with-parent", "--new-session"):
        assert flag in command
    assert command[command.index("--proc") + 1] == "/proc"
    assert command[command.index("--tmpfs") + 1] == "/tmp"  # noqa: S108

    def bound(kind: str) -> list[str]:
        return [command[i + 1] for i, item in enumerate(command) if item == kind]

    assert str(work) in bound("--bind") and str(home) in bound("--bind")
    assert str(modules.resolve()) in bound("--ro-bind-try")
    assert "/usr" in bound("--ro-bind") and "/etc" in bound("--ro-bind")
    # No home directory of anybody's, and nothing under /tmp beyond the
    # workspace and HOME themselves.
    for path in bound("--ro-bind") + bound("--ro-bind-try"):
        assert not path.startswith(("/root", "/home")), path
    assert command[-2:] == ["opencode", "run"]
    assert command[command.index("--chdir") + 1] == str(work)


def test_the_api_is_visible_but_not_the_directory_that_holds_its_env():
    """The documentation tool is `python -m basivo_orch...`, so the venv and
    the package are bound. Their parent is where `.env` lives, and it is not."""
    venv, package = jail._api_read_only()
    assert Path(sys.executable).resolve().is_relative_to(venv)
    assert package.name == "basivo_orch"
    assert venv.parent not in (venv, package)


@pytest.mark.skipif(
    platform.system() != "Darwin" or not jail._find("sandbox-exec"),
    reason="the macOS jail",
)
def test_the_macos_jail_confines_reads_and_writes_for_real(tmp_path, monkeypatch):
    monkeypatch.setattr(jail, "MODE", "auto")
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (work / "inside.txt").write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    def run(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 - our own argv, in a test
            jail.wrap(list(argv), workspace=work, home=home),
            capture_output=True,
            timeout=30,
            check=False,
            cwd=work,
        )

    assert run("/bin/cat", str(work / "inside.txt")).stdout == b"inside"
    denied = run("/bin/cat", str(outside))
    assert denied.returncode != 0 and b"not permitted" in denied.stderr
    written = run("/bin/sh", "-c", f"echo x > {tmp_path}/escaped.txt")
    assert written.returncode != 0 and not (tmp_path / "escaped.txt").exists()
    # And the worker's own configuration is out of reach.
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        assert run("/bin/cat", str(env_file)).returncode != 0


@pytest.mark.skipif(
    platform.system() != "Linux" or not jail._find("bwrap"),
    reason="the Linux jail",
)
def test_the_linux_jail_confines_reads_and_hides_the_parent_environment(tmp_path, monkeypatch):
    if jail.tool() != "bwrap":
        pytest.skip("bwrap is installed but user namespaces are not allowed here")
    monkeypatch.setattr(jail, "MODE", "auto")
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (work / "inside.txt").write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    def run(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 - our own argv, in a test
            jail.wrap(list(argv), workspace=work, home=home),
            capture_output=True,
            timeout=30,
            check=False,
        )

    assert run("/bin/cat", str(work / "inside.txt")).stdout == b"inside"
    assert run("/bin/cat", str(outside)).returncode != 0
    # A PID namespace of its own: the worker's process, and with it the
    # environment holding the database URL, is not in the agent's /proc.
    import os

    assert run("/bin/cat", f"/proc/{os.getpid()}/environ").returncode != 0


def test_the_private_tmp_is_mounted_before_the_workspace_is_bound(tmp_path):
    """On Linux both the workspace and the agent's home are under /tmp, and
    bwrap applies its arguments in order: a tmpfs emitted after the binds
    lands on top of them, and the agent cannot chdir into its own project."""
    workspace = tmp_path / "app"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    argv = jail._bwrap(["agent"], workspace=workspace, home=home, read_only=[])
    tmpfs = argv.index("--tmpfs")
    binds = [i for i, item in enumerate(argv) if item == "--bind"]

    assert binds, "the workspace and the home are bound"
    assert tmpfs < min(binds), "the tmpfs would cover the workspace"
    assert argv.index("--proc") < min(binds)
    # And the agent still starts inside its project.
    assert argv[argv.index("--chdir") + 1] == str(workspace)
