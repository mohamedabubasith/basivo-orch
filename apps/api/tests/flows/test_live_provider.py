"""Live-provider test: a real agent run against a real model API.

Opt-in, because CI has no API key and a suite that needs one would either fail
everywhere or push people to commit secrets. Provide the environment and it
runs; omit it and the whole file skips with a message saying how to enable it:

    BASIVO_LIVE_API_KEY=sk-...              # required to enable
    BASIVO_LIVE_PROVIDER=openai             # default: openai (any registry name)
    BASIVO_LIVE_MODEL=gpt-4o-mini           # default shown
    BASIVO_LIVE_BASE_URL=https://...        # optional — OpenAI-compatible hosts
                                            # (NVIDIA, Groq-compatible proxies…)

This is the "realtime" half of the testing story: the FunctionModel suites
prove the loop, tools and logging; this proves the provider wiring — auth
header, wire format, usage extraction — against the genuine article.
"""

from __future__ import annotations

import os

import pytest

from basivo_orch.flows.engine import Engine
from basivo_orch.flows.events import replay
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import RunStatus

LIVE_KEY = os.environ.get("BASIVO_LIVE_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not LIVE_KEY,
    reason="Live provider test: set BASIVO_LIVE_API_KEY (and optionally "
    "BASIVO_LIVE_PROVIDER/MODEL/BASE_URL) to run a real agent call.",
)


async def test_a_real_agent_run_end_to_end(session, make_run, monkeypatch):
    # AgentConfig deliberately has no api_key field — keys live in stored
    # credentials or the provider SDK's own environment lookup. A test has no
    # workspace to store a credential in, so it uses the env route: hand the
    # key to whichever SDK the chosen provider resolves to.
    provider = os.environ.get("BASIVO_LIVE_PROVIDER", "openai")
    for env_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(env_name, LIVE_KEY)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "agent",
                    "type": "agent.llm",
                    "config": {
                        "provider": provider,
                        "model": os.environ.get("BASIVO_LIVE_MODEL", "gpt-4o-mini"),
                        "base_url": os.environ.get("BASIVO_LIVE_BASE_URL", ""),
                        "prompt": "Reply with exactly the word: pong",
                        "max_tokens": 16,
                    },
                },
            ],
            "edges": [{"source": "t", "target": "agent"}],
        }
    )

    run = await make_run(graph, None)
    run = await Engine(session, run=run, graph=graph, redis_client=None).execute()

    assert run.status is RunStatus.SUCCEEDED, run.error
    text = run.output["result"]["text"]
    assert "pong" in text.lower()

    # The observability claims must hold against a real provider too: tokens
    # counted, and the model turn recorded as a step in the persisted log.
    usage = run.output["result"]["usage"]
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0

    events = await replay(session, run.id)
    steps = [e.data["step"] for e in events if e.type == "node.step"]
    assert "llm.response" in steps
    assert "agent.finished" in steps
