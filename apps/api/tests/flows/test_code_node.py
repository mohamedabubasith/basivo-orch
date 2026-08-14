"""The Code node.

Real subprocesses, no mocks: the value of these tests is that the wrapper
protocol — code in via stdin, result out via the real stdout, prints diverted —
actually survives contact with a CPython interpreter, including the ways user
code goes wrong: prints, exceptions, no main(), unserialisable returns, spins.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.code import CodeConfig, CodeNode


class _Recorder:
    def __init__(self) -> None:
        self.progress_lines: list[str] = []
        self.steps: list[tuple[str, dict]] = []

    async def progress(self, message: str) -> None:
        self.progress_lines.append(message)

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))


def make_context(recorder: _Recorder, http: httpx.AsyncClient, *, upstream: object) -> NodeContext:
    async def resolve_credential(_credential_id: str):
        return None

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="code_1",
        node_name="Code",
        attempt=1,
        input=upstream,
        outputs={"earlier": {"output": {"x": 1}}},
        variables={"env": "test"},
        trigger={"payload": {"n": 21}},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=resolve_credential,
        http=http,
    )


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


async def test_main_sees_the_template_context_and_its_return_is_the_output(http_client):
    config = CodeConfig(
        code=(
            "def main(data):\n"
            "    return {\n"
            '        "doubled": data["input"]["n"] * 2,\n'
            '        "var": data["vars"]["env"],\n'
            '        "from_earlier": data["nodes"]["earlier"]["output"]["x"],\n'
            "    }\n"
        )
    )
    recorder = _Recorder()
    ctx = make_context(recorder, http_client, upstream={"n": 21})

    result = await CodeNode().run(config, ctx)

    assert result.output == {"doubled": 42, "var": "test", "from_earlier": 1}


async def test_user_prints_do_not_corrupt_the_result_and_surface_as_progress(http_client):
    config = CodeConfig(code='def main(data):\n    print("debugging here")\n    return "ok"\n')
    recorder = _Recorder()
    ctx = make_context(recorder, http_client, upstream={})

    result = await CodeNode().run(config, ctx)

    assert result.output == "ok"
    assert any("debugging here" in line for line in recorder.progress_lines)


async def test_an_exception_surfaces_the_traceback_tail(http_client):
    config = CodeConfig(code="def main(data):\n    return 1 / 0\n")
    ctx = make_context(_Recorder(), http_client, upstream={})

    with pytest.raises(NodeError) as raised:
        await CodeNode().run(config, ctx)
    assert "ZeroDivisionError" in str(raised.value)


async def test_missing_main_is_a_clear_error(http_client):
    config = CodeConfig(code="x = 1\n")
    ctx = make_context(_Recorder(), http_client, upstream={})

    with pytest.raises(NodeError) as raised:
        await CodeNode().run(config, ctx)
    assert "must define a function main" in str(raised.value)


async def test_an_unserialisable_return_is_a_clear_error(http_client):
    # default=str catches most exotic values, so force one json cannot walk:
    # a self-referential structure recurses forever without the guard.
    config = CodeConfig(code="def main(data):\n    a = []\n    a.append(a)\n    return a\n")
    ctx = make_context(_Recorder(), http_client, upstream={})

    with pytest.raises(NodeError) as raised:
        await CodeNode().run(config, ctx)
    assert "Code failed" in str(raised.value) or "no readable result" in str(raised.value)


async def test_a_wall_clock_hang_is_killed(http_client):
    config = CodeConfig(
        code="import time\ndef main(data):\n    time.sleep(30)\n", timeout_seconds=1
    )
    ctx = make_context(_Recorder(), http_client, upstream={})

    with pytest.raises(NodeError) as raised:
        await CodeNode().run(config, ctx)
    assert "did not finish within" in str(raised.value)


async def test_isolated_mode_hides_the_server_environment(http_client, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_SERVER_VALUE", "leak-me")
    config = CodeConfig(
        code='import os\ndef main(data):\n    return os.environ.get("SUPER_SECRET_SERVER_VALUE")\n'
    )
    ctx = make_context(_Recorder(), http_client, upstream={})

    result = await CodeNode().run(config, ctx)

    # The subprocess is spawned with env={} precisely because the parent holds
    # SECRET_KEY, DATABASE_URL and the credential master key in its
    # environment — none of which is a code node's business.
    assert result.output is None
