"""Templates are installed by people who will not read them.

That is the whole point of one, and it is also the risk: a template that no
longer validates fails on someone's first message, on a node they have never
opened, in a product they are still deciding whether to trust. So the graph is
checked here the same way the API checks it at publish, and the wiring the bot
depends on is asserted rather than assumed.
"""

from __future__ import annotations

import json
import uuid

import pytest

from basivo_orch.flows import nodes as registry
from basivo_orch.flows.graph import Graph, GraphError, validate_graph
from basivo_orch.flows.templates import TEMPLATES

TELEGRAM = str(uuid.uuid4())
LLM = str(uuid.uuid4())


def built(name: str) -> Graph:
    return Graph.model_validate(
        TEMPLATES[name].build(telegram_credential_id=TELEGRAM, llm_credential_id=LLM)
    )


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_a_template_is_a_flow_that_would_publish(name: str):
    validate_graph(built(name), known_types=registry.REGISTRY)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_a_template_declares_what_it_needs(name: str):
    template = TEMPLATES[name]
    assert template.title and template.summary and template.detail
    assert template.needs, "an install screen has to know what to ask for"


def test_the_credentials_reach_every_node_that_needs_one():
    """The failure this prevents: a template that installs, looks finished, and
    fails on its first message with "pick a credential"."""
    graph = built("studio-video-bot")
    for node in graph.nodes:
        if node.type in {"telegram.reply", "trigger.telegram"}:
            assert node.config.get("credential_id") == TELEGRAM, node.id
        if node.type == "agent.llm":
            assert node.config.get("credential_id") == LLM, node.id


def test_every_branch_of_the_bot_answers():
    """A bot that silently does nothing is indistinguishable from a broken one,
    and the operator's next move is to send everything again."""
    graph = built("studio-video-bot")
    replies = {node.id for node in graph.nodes if node.type == "telegram.reply"}

    # Walk from each condition's two ports and check a reply is reachable.
    outgoing: dict[str, list] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge.source, []).append(edge)

    def reaches_reply(start: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        if start in seen:
            return False
        seen.add(start)
        if start in replies:
            return True
        return any(reaches_reply(edge.target, seen) for edge in outgoing.get(start, []))

    for node in graph.nodes:
        if node.type != "logic.condition":
            continue
        ports = {edge.source_handle for edge in outgoing.get(node.id, [])}
        assert ports == {"true", "false"}, f"{node.id} leaves one branch unwired: {ports}"
        for edge in outgoing[node.id]:
            assert reaches_reply(edge.target), (
                f"{node.id} on '{edge.source_handle}' never reaches a reply"
            )


def test_the_render_branch_holds_and_releases_the_lock():
    """Two taps of Try again must not start two renders, and a lock that is
    taken and never released leaves the bot mute until it expires."""
    graph = built("studio-video-bot")
    actions = [
        (node.id, node.config.get("action")) for node in graph.nodes if node.type == "session.state"
    ]
    assert ("claim", "lock") in actions
    assert ("unlock", "unlock") in actions

    # And the unlock is downstream of the delivery, not beside it.
    order = {node.id: index for index, node in enumerate(graph.nodes)}
    assert order["unlock"] > order["deliver"]


def test_the_photo_branch_stores_what_arrived():
    graph = built("studio-video-bot")
    keep = next(node for node in graph.nodes if node.id == "keep")
    # Addressed to the trigger by name: `keep` sits after a condition, whose
    # output carries no photographs.
    assert keep.config["artifact_id"] == "{{ nodes.inbox.output.photos.0.artifact_id }}"
    assert keep.config["file_unique_id"] == "{{ nodes.inbox.output.photos.0.file_unique_id }}", (
        "without this the same photograph forwarded twice is collected twice"
    )


def test_an_unknown_template_is_not_a_silent_empty_flow():
    with pytest.raises(KeyError):
        built("no-such-template")


def test_the_bot_survives_a_missing_llm_credential():
    """A studio that has not added a model provider yet still gets a flow they
    can look at, rather than a 500 during install."""
    graph = Graph.model_validate(
        TEMPLATES["studio-video-bot"].build(telegram_credential_id=TELEGRAM)
    )
    # It will not *run* the agent without one, but it has to be a valid graph.
    try:
        validate_graph(graph, known_types=registry.REGISTRY)
    except GraphError as error:  # pragma: no cover - shown when it regresses
        pytest.fail(f"install would fail: {error.problems}")


def test_no_node_reads_input_from_something_that_cannot_supply_it():
    """`input` is the UPSTREAM NODE's output, not the trigger's.

    This has now been the cause of three separate failures: a reply after a
    condition asking for `input.chat_id`, a condition after a condition asking
    for `input.command`, and a session node after a condition asking for
    `input.photos`. A condition emits {result, comparisons} and nothing else,
    so anything downstream of one has to name the node it means.

    The symptom is not a crash at install or publish. It is a bot that answers
    the wrong branch, days later, with a message the operator cannot make sense
    of.
    """
    import re

    graph = built("studio-video-bot")
    by_id = {node.id: node for node in graph.nodes}
    upstream: dict[str, list[str]] = {}
    for edge in graph.edges:
        upstream.setdefault(edge.target, []).append(edge.source)

    # What each node type actually puts in its output, for the fields a
    # template is likely to ask for.
    supplies = {
        "logic.condition": {"result", "comparisons"},
        "trigger.telegram": {
            "chat_id", "kind", "text", "command", "callback_data", "callback_id",
            "photos", "message_id", "media_group_id", "user", "chat_type",
        },
        "session.state": {
            "chat_id", "state", "photos", "photo_count", "photo_ids", "brief",
            "options", "status_message_id", "last_video_artifact_id", "iteration",
            "spend_usd", "locked", "acquired", "added", "duplicate", "is_new",
        },
        "telegram.reply": {"message_id", "chat_id", "sent"},
        "agent.llm": {"text", "json", "stop_reason", "handover_to", "usage"},
        "video.invitation": {"artifact_id", "url", "seconds", "width", "height"},
    }

    problems: list[str] = []
    for node in graph.nodes:
        wanted = set(re.findall(r"\{\{ input\.([a-z_]+)", json.dumps(node.config)))
        if not wanted:
            continue
        sources = upstream.get(node.id, [])
        assert sources, f"{node.id} reads input but nothing feeds it"
        for source in sources:
            available = supplies.get(by_id[source].type, set())
            missing = wanted - available
            if missing:
                problems.append(
                    f"{node.id} reads input.{'/'.join(sorted(missing))} "
                    f"but its upstream {source} ({by_id[source].type}) does not supply it"
                )

    assert not problems, "\n  ".join(problems)
