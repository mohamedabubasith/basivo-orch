"""Running Claude Code as the repair engine.

The builtin repair loop hands a model four tools — list, find, read, write —
over the host's HTTP API. Claude Code is a far stronger coding agent than that
loop will ever be: it plans, greps, reads selectively, edits in place, and has
been tuned on exactly this job. When the person has given us an Anthropic key,
refusing to use it would be leaving the best tool in the box.

It runs as a subprocess, headless, inside a directory that holds a copy of the
repository. Three things about how it runs are the whole safety story:

**Its tools are restricted to files.** Read, Edit, Write, Glob, Grep. No Bash,
no web. The problem text comes from a bug report anyone may have written, and a
coding agent that can run commands is a remote shell for whoever writes the
issue. File edits are reviewed before anything is pushed (see `gitops`).

**The key reaches it as an environment variable and nowhere else.** Not on the
command line (visible in `ps`), not in a config file, not in any log line this
module writes. Errors are scrubbed of it before they are raised.

**It gets its own HOME.** Claude Code writes settings, session transcripts and
update checks under the home directory. In a worker that is shared state
between runs and between tenants; a throwaway directory per run is not.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from basivo_orch.flows.nodes.base import NodeError

#: Everything the repair job needs and nothing that can reach outside the
#: working copy. Bash is the notable absence, deliberately.
FILE_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Glob", "Grep")
#: Refused even if a future default were to allow them.
DENIED_TOOLS: tuple[str, ...] = ("Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit")


@dataclass
class ClaudeCodeResult:
    text: str
    cost_usd: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    session_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def binary() -> str | None:
    """Where the CLI is, or None. Env override for a non-standard install."""
    return os.environ.get("BASIVO_CLAUDE_CODE_BIN") or shutil.which("claude")


def is_subscription_token(secret: str) -> bool:
    """Tokens from `claude setup-token` carry this prefix; API keys do not.

    Only Claude Code accepts them. The Messages API behind the AI Agent node
    does not, so a credential holding one drives repairs and nothing else.
    """
    return secret.startswith("sk-ant-oat")


def redact(text: str, secret: str) -> str:
    """The key must never land in a log line or an error message."""
    return text.replace(secret, "[redacted]") if secret else text


async def run_claude_code(
    *,
    cwd: Path,
    prompt: str,
    system_prompt: str,
    api_key: str,
    base_url: str | None = None,
    model: str = "",
    max_turns: int = 30,
    max_budget_usd: float | None = None,
    timeout_seconds: float = 780.0,
) -> ClaudeCodeResult:
    """One headless Claude Code session over `cwd`. Returns what it said and cost.

    The prompt travels on stdin: it carries a whole bug report and can run to
    kilobytes, and an argument that long is visible in the process table and
    hits ARG_MAX on a bad day.
    """
    executable = binary()
    if executable is None:
        raise NodeError(
            "Claude Code is not installed on this worker. The image installs it as "
            "`@anthropic-ai/claude-code`; set BASIVO_CLAUDE_CODE_BIN if it lives elsewhere."
        )

    argv = [
        executable,
        "-p",
        # No hooks, skills or settings from the host: the worker's own Claude
        # Code configuration, if any, must not shape a tenant's repair.
        "--bare",
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        ",".join(FILE_TOOLS),
        "--disallowedTools",
        ",".join(DENIED_TOOLS),
        "--max-turns",
        str(max_turns),
        "--append-system-prompt",
        system_prompt,
    ]
    if model:
        argv += ["--model", model]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{max_budget_usd:.2f}"]

    # A throwaway HOME: settings, transcripts and caches land here and are
    # deleted with it, so nothing persists between runs or tenants.
    with tempfile.TemporaryDirectory(prefix="basivo-claude-home-") as home:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": home,
            "CLAUDE_CONFIG_DIR": home,
            # A Pro or Max subscription has no API key. `claude setup-token`
            # on the person's own machine mints a long-lived token for exactly
            # this headless use; Claude Code reads it from a different variable.
            (
                "CLAUDE_CODE_OAUTH_TOKEN" if is_subscription_token(api_key) else "ANTHROPIC_API_KEY"
            ): api_key,
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode()), timeout=timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise NodeError(
                f"Claude Code did not finish within {timeout_seconds:.0f}s. Narrow the "
                "problem statement, or raise the node's timeout."
            ) from None

    out = redact(stdout.decode(errors="replace"), api_key)
    err = redact(stderr.decode(errors="replace"), api_key)

    payload = _final_json(out)
    if process.returncode != 0 and payload is None:
        raise NodeError(
            f"Claude Code exited with status {process.returncode}: "
            f"{(err or out).strip()[-600:] or 'no output'}"
        )
    if payload is None:
        raise NodeError(f"Claude Code returned no JSON result. Output tail:\n{out[-600:]}")
    if process.returncode != 0:
        # Exit 2 is "partial": the cost ceiling or an auth failure stopped it
        # mid-run. Edits may exist on disk; pushing half a fix is worse than
        # none, so this is a failure with the agent's own words attached.
        raise NodeError(
            f"Claude Code stopped early (exit {process.returncode}): "
            f"{str(payload.get('result') or err.strip())[-600:]}"
        )

    result = ClaudeCodeResult(
        text=str(payload.get("result") or ""),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        turns=int(payload.get("num_turns") or 0),
        input_tokens=int((payload.get("usage") or {}).get("input_tokens") or 0),
        output_tokens=int((payload.get("usage") or {}).get("output_tokens") or 0),
        duration_ms=int(payload.get("duration_ms") or 0),
        session_id=str(payload.get("session_id") or ""),
        raw=payload,
    )
    if payload.get("is_error"):
        raise NodeError(
            f"Claude Code reported an error ({payload.get('subtype', 'error')}): "
            f"{result.text[:600] or err.strip()[-600:]}"
        )
    return result


def _final_json(output: str) -> dict[str, Any] | None:
    """The result object, wherever it sits in the output.

    `--output-format json` prints one object, but a stray warning line before it
    is not unheard of, so the last parseable object wins rather than the whole
    stream having to be clean.
    """
    text = output.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


# ---------------------------------------------------------------------------
# The working copy
# ---------------------------------------------------------------------------

#: Where report screenshots are written for the agent to Read. Excluded from
#: the change set, so they never end up committed.
REPORT_DIR = ".basivo-report"


def extract_archive(archive: bytes, into: Path) -> Path:
    """Unpack a host's tar.gz snapshot and return the repository root.

    Both GitHub and GitLab wrap the tree in one top-level directory named after
    the commit; the caller wants the tree. `filter="data"` is what makes a
    hostile archive — absolute paths, `..`, symlinks out — refuse to extract
    rather than write outside `into`.
    """
    import io
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            tar.extractall(into, filter="data")
    except tarfile.TarError as exc:
        raise NodeError(
            f"The repository archive was refused: {exc}. A path in it would have written "
            "outside the working directory."
        ) from None
    children = [child for child in into.iterdir() if child.is_dir()]
    if len(children) == 1:
        return children[0]
    return into


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under `root` with its content, keyed by repo-relative path."""
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(REPORT_DIR + "/") or relative.startswith(".git/"):
            continue
        files[relative] = path.read_bytes()
    return files


def changed_files(before: dict[str, bytes], after: dict[str, bytes]) -> dict[str, bytes | None]:
    """What the agent did: path → new bytes, or None for a deletion."""
    changes: dict[str, bytes | None] = {}
    for path, content in after.items():
        if before.get(path) != content:
            changes[path] = content
    for path in before:
        if path not in after:
            changes[path] = None
    return changes
