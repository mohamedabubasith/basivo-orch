"""The rewrite that keeps saved flows runnable after the media nodes merged.

Seven node types became two. A graph in the database names its nodes by type,
so without this rewrite every flow built before the change would open with a
node the editor cannot draw and fail the moment it ran.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from basivo_orch.flows import nodes as registry

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "e51a0b7cc4d2_two_media_nodes.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("media_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration():
    return _module()


def test_every_retired_type_maps_to_a_type_that_exists(migration):
    """A mapping onto a node that is also gone would only move the failure."""
    for old in migration.VIDEO_TYPES:
        assert old not in registry.REGISTRY, f"{old} is still registered"
    assert "video.ai" in registry.REGISTRY
    assert "image.ai" in registry.REGISTRY


def test_a_montage_becomes_a_video_with_its_photos_and_a_brief(migration):
    graph, changed = migration._rewrite(
        {
            "nodes": [
                {"id": "t", "type": "trigger.telegram", "config": {}},
                {
                    "id": "film",
                    "type": "video.montage",
                    "name": "Montage",
                    "config": {
                        "artifact_ids": "{{ trigger.photo_ids }}",
                        "duration_seconds": 12,
                    },
                },
            ],
            "edges": [{"source": "t", "target": "film"}],
        }
    )

    assert changed
    film = graph["nodes"][1]
    assert film["type"] == "video.ai"
    assert film["config"]["photos"] == "{{ trigger.photo_ids }}"
    assert film["config"]["duration_seconds"] == 12
    assert "montage" in film["config"]["brief"].lower()
    # And the result validates against the node it is now claiming to be.
    registry.REGISTRY["video.ai"].config_model.model_validate(film["config"])


def test_a_poster_keeps_its_layout_as_the_brief(migration):
    graph, _ = migration._rewrite(
        {
            "nodes": [
                {
                    "id": "poster",
                    "type": "design.render",
                    "config": {"html": "<h1>Sale</h1>", "size": "story"},
                }
            ],
            "edges": [],
        }
    )

    poster = graph["nodes"][0]
    assert poster["type"] == "image.ai"
    assert "<h1>Sale</h1>" in poster["config"]["brief"]
    assert poster["config"]["size"] == "story"
    registry.REGISTRY["image.ai"].config_model.model_validate(poster["config"])


def test_a_voice_node_is_removed_and_the_flow_is_wired_around_it(migration):
    """There is nowhere for a standalone voice to go, so the chain has to close
    over the gap: leaving it would strand every node downstream."""
    graph, changed = migration._rewrite(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {"id": "say", "type": "audio.speak", "config": {"text": "hi"}},
                {"id": "post", "type": "social.post", "config": {}},
            ],
            "edges": [
                {"source": "t", "target": "say"},
                {"source": "say", "target": "post"},
            ],
        }
    )

    assert changed
    assert [node["id"] for node in graph["nodes"]] == ["t", "post"]
    assert graph["edges"] == [{"source": "t", "target": "post"}]


def test_a_graph_with_nothing_retired_is_left_alone(migration):
    original = {
        "nodes": [{"id": "t", "type": "trigger.manual", "config": {}}],
        "edges": [],
    }
    graph, changed = migration._rewrite(original)
    assert changed is False
    assert graph == original
