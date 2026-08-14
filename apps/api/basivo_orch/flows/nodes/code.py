"""The Code node — the author's own Python, run as one step of the flow.

Every orchestrator of this kind ends up needing one: the moment a flow has to
reshape data in a way no built-in node anticipated, the choice is between a
code node and the user abandoning the product. The contract is one function:

    def main(data):
        return {"greeting": f"hello {data['input']['name']}"}

`data` carries the same four keys templates see — `input` (the upstream
node's output), `nodes`, `vars`, `trigger` — and whatever `main` returns
becomes this node's output, addressable downstream as usual.

**What the sandbox is and is not.** The code runs in a separate interpreter
(`python -I`: isolated mode — no site-packages, no user site, no environment
`PYTHON*` variables), with a CPU limit, a wall-clock timeout, and its stdout
diverted so stray `print()`s cannot corrupt the result protocol. That is
containment against accidents — infinite loops, runaway memory of the CPU-bound
kind, print debugging — not a security boundary against a hostile author. The
code executes with the server's OS privileges, exactly as n8n's Function node
or a CI pipeline step does. The trust model matches: flow authors are
authenticated workspace members who could already reach anything the HTTP node
can reach; a *tenant* boundary this is not, and multi-tenant deployments that
let strangers author flows should disable the node until it runs in a real
jail (gVisor, Firecracker) — tracked, not pretended.
"""

from __future__ import annotations

import asyncio
import json
import resource
import sys
from typing import Any

from pydantic import BaseModel, Field

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult

#: Runs inside the child interpreter. The user's code arrives on stdin next to
#: the data — never interpolated into this string, so no quoting of user code
#: can break out of it — and the result leaves on the *real* stdout, which is
#: captured before user code gets a chance to print to it.
_WRAPPER = """\
import json, sys, traceback

_real_stdout = sys.stdout
# User print()s go to stderr: visible in the node's error/log output when
# something fails, and never able to corrupt the JSON result channel.
sys.stdout = sys.stderr

_payload = json.loads(sys.stdin.read())
_scope = {"__name__": "__basivo_code__"}
try:
    exec(compile(_payload["code"], "<code node>", "exec"), _scope)
except Exception:
    traceback.print_exc()
    sys.exit(3)

_main = _scope.get("main")
if not callable(_main):
    print("The code must define a function main(data).", file=sys.stderr)
    sys.exit(4)

try:
    _result = _main(_payload["data"])
except Exception:
    traceback.print_exc()
    sys.exit(5)

try:
    json.dump({"result": _result}, _real_stdout, default=str)
except (TypeError, ValueError):
    print("main() returned something JSON cannot represent.", file=sys.stderr)
    sys.exit(6)
"""


class CodeConfig(BaseModel):
    model_config = {"extra": "forbid"}

    code: str = Field(
        default='def main(data):\n    return data["input"]\n',
        max_length=50_000,
        description="Must define main(data). Its return value becomes this node's output.",
    )
    timeout_seconds: float = Field(default=10.0, ge=1, le=60)


class CodeNode(Node):
    type = "code.python"
    label = "Python Code"
    description = (
        "Run your own Python. main(data) receives input, nodes, vars and "
        "trigger; whatever it returns becomes the node's output."
    )
    tier = 1
    category = "utility"
    config_model = CodeConfig

    #: Retrying deterministic code re-runs the same bug; it never helps.
    max_attempts = 1
    timeout_seconds = 70.0

    async def run(self, config: CodeConfig, ctx: NodeContext) -> NodeResult:
        payload = json.dumps(
            {"code": config.code, "data": ctx.template_context()}, default=str
        ).encode("utf-8")

        def limit_resources() -> None:
            # CPU seconds, not wall seconds: a sleep() is harmless, a spin loop
            # is killed by the kernel even if the wall timeout somehow fails.
            cpu = int(config.timeout_seconds) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # isolated: no site-packages, no PYTHON* env, no cwd on path
            "-c",
            _WRAPPER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=limit_resources,
            # An empty environment, not the server's. The parent process holds
            # SECRET_KEY, DATABASE_URL and every provider credential's master
            # key in its env — one os.environ read away from any code node.
            # The interpreter itself needs nothing from the environment when
            # exec'd by absolute path with -I.
            env={},
        )

        try:
            # A grace second over the configured limit: the CPU rlimit and this
            # wall timeout race, and the rlimit's kill produces the better
            # error (a traceback) when the code is genuinely spinning.
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=config.timeout_seconds + 2
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise NodeError(
                f"The code did not finish within {config.timeout_seconds:g}s and was stopped."
            ) from None

        printed = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            # The tail is where Python puts the actual exception; the head of a
            # long traceback is wrapper frames the author did not write.
            tail = "\n".join(printed.splitlines()[-15:]) or f"exit code {process.returncode}"
            raise NodeError(f"Code failed:\n{tail}")

        if printed:
            # Their print() output — worth surfacing, it is how people debug.
            await ctx.progress(printed[-500:])

        try:
            result: Any = json.loads(stdout.decode("utf-8"))["result"]
        except (ValueError, KeyError) as exc:
            raise NodeError("The code produced no readable result.") from exc

        return NodeResult(output=result)
