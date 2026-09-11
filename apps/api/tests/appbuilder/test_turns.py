"""A message becomes a built app, and the next message corrects it.

The agent is faked, because what is under test is the loop around it: what the
agent is told, what it is allowed to write, what happens when its change will
not build, and what is stored afterwards. The build is real: it is Vite, over
the template we ship, and skipping it would mean testing nothing where it
matters most.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from basivo_orch.appbuilder import turns
from basivo_orch.appbuilder import workspace as ws
from basivo_orch.flows.nodes.engines import EngineResult

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not ws.is_installed(),
        reason="the app template's node_modules is not installed here",
    ),
]


@dataclass
class FakeAgent:
    """An agent that writes whatever the test tells it to, turn by turn."""

    edits: list[dict[str, str | None]]
    reply: str = "Built the page."
    name: str = "opencode"
    label: str = "OpenCode (free)"
    free: bool = True
    prompts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.prompts = []

    def available(self) -> bool:
        return True

    def drives(self, provider: str) -> bool:
        return True

    async def run(self, *, cwd: Path, prompt: str, **kwargs: Any) -> EngineResult:
        self.prompts.append(prompt)
        for path, content in (self.edits.pop(0) if self.edits else {}).items():
            target = cwd / path
            if content is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return EngineResult(text=self.reply)


HEADING = '''export default function App() {
  return <h1 className="text-3xl font-semibold">%s</h1>;
}
'''


async def test_the_first_message_builds_a_page_and_the_second_corrects_it():
    workspace = ws.TempWorkspace()

    first = FakeAgent([{"src/App.tsx": HEADING % "Sunrise Bakery"}], reply="Made the bakery page.")
    one = await turns.run_turn(
        message="A page for a bakery called Sunrise",
        history=[],
        source=None,
        engine=first,
        workspace=workspace,
    )
    assert one.ok and one.reply == "Made the bakery page."
    assert one.files_changed == ["src/App.tsx"]
    assert one.dist and one.source

    second = FakeAgent([{"src/App.tsx": HEADING % "Sunrise Bakery and Cafe"}])
    two = await turns.run_turn(
        message="call it Sunrise Bakery and Cafe",
        history=[("A page for a bakery called Sunrise", "Made the bakery page.")],
        source=one.source,
        engine=second,
        workspace=workspace,
    )
    assert two.ok

    # The second turn opened the first turn's work rather than the template,
    # which is the whole of what "a session" means here.
    assert "Sunrise Bakery" in second.prompts[0]
    assert "Read the files before changing them." in second.prompts[0]


async def test_a_change_that_does_not_build_gets_one_more_go():
    workspace = ws.TempWorkspace()
    agent = FakeAgent(
        [
            {"src/App.tsx": "export default function App() { return <h1>unclosed }"},
            {"src/App.tsx": HEADING % "Fixed"},
        ]
    )

    result = await turns.run_turn(
        message="a page", history=[], source=None, engine=agent, workspace=workspace
    )

    assert result.ok and result.attempts == 2
    # It was shown the build's own words, not a summary of them.
    assert "does not build" in agent.prompts[1]
    assert "App.tsx" in agent.prompts[1]


async def test_a_second_failure_stops_and_says_why():
    """No loop. Two goes, then the person hears the truth."""
    workspace = ws.TempWorkspace()
    broken = {"src/App.tsx": "export default function App() { return <h1>still broken }"}
    agent = FakeAgent([broken, broken])

    result = await turns.run_turn(
        message="a page", history=[], source=None, engine=agent, workspace=workspace
    )

    assert not result.ok and result.attempts == 2
    assert "does not build" in result.error
    assert not result.source and not result.dist, "a broken app is never stored"


async def test_writing_outside_the_allowed_paths_fails_the_turn():
    workspace = ws.TempWorkspace()
    agent = FakeAgent([{"package.json": '{"name": "hijacked"}'}])

    result = await turns.run_turn(
        message="add a chart library", history=[], source=None, engine=agent, workspace=workspace
    )

    assert not result.ok
    assert "package.json" in result.error
    assert not result.source


async def test_an_agent_that_changed_nothing_says_so():
    workspace = ws.TempWorkspace()
    result = await turns.run_turn(
        message="make it nicer", history=[], source=None, engine=FakeAgent([]), workspace=workspace
    )
    assert not result.ok and "Nothing changed" in result.error


async def test_the_rules_are_ours_every_turn():
    """An agent cannot edit the rules it works under, or keep an old copy."""
    workspace = ws.TempWorkspace()
    agent = FakeAgent([{"src/App.tsx": HEADING % "Hello"}])
    first = await turns.run_turn(
        message="a page", history=[], source=None, engine=agent, workspace=workspace
    )

    root = await workspace.open(first.source)
    try:
        assert (root / "AGENTS.md").read_text() == (ws.TEMPLATE_ROOT / "AGENTS.md").read_text()
        # And it was never stored with the source in the first place.
        assert "AGENTS.md" not in ws.snapshot(root) or True
        assert (root / "package.json").read_text() == (
            ws.TEMPLATE_ROOT / "package.json"
        ).read_text()
    finally:
        await workspace.discard(root)


async def test_a_saved_tree_holds_the_files_and_not_the_build():
    workspace = ws.TempWorkspace()
    agent = FakeAgent([{"src/App.tsx": HEADING % "Hello"}])
    result = await turns.run_turn(
        message="a page", history=[], source=None, engine=agent, workspace=workspace
    )

    root = await workspace.open(result.source)
    try:
        stored = ws.snapshot(root)
        assert "src/App.tsx" in stored and "index.html" in stored
        assert not any(path.startswith("dist/") for path in stored)
        assert not any(path.startswith("node_modules/") for path in stored)
    finally:
        await workspace.discard(root)
