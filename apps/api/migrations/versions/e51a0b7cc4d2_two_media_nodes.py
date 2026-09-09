"""fold seven media nodes into AI Image and AI Video

Revision ID: e51a0b7cc4d2
Revises: d4c1a7b90e21
Create Date: 2026-09-09 23:55:00.000000

Saved graphs name node types as strings, so removing a type would leave a flow
that cannot be opened or run: the editor draws an unknown node and the engine
refuses the graph. This rewrites the ones that were folded in.

The mapping is the honest one, not a clever one. Every removed video node made
a video and every removed image node made an image, so each becomes the AI
node of that kind with a brief written from whatever the old node was going to
show. A stranded `audio.speak` cannot become anything — the voice is now a tick
box on the video node — so its node is removed and whatever it fed is wired to
whatever fed it, which keeps the rest of the flow intact.

Downgrade is not possible for the same reason a rewrite was needed: the old
nodes' settings do not exist in the new ones. It is a no-op that says so.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "e51a0b7cc4d2"
down_revision: str | None = "d4c1a7b90e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What each retired node became, and the brief it starts life with.
VIDEO_TYPES = {"video.render", "video.generate", "video.montage", "video.invitation"}
IMAGE_TYPES = {"design.render", "image.edit"}


def _brief(node: dict[str, Any]) -> str:
    """A brief written from what the old node was configured to produce."""
    config = node.get("config") or {}
    if isinstance(config.get("brief"), str) and config["brief"].strip():
        return config["brief"]

    name = str(node.get("name") or "").strip()
    old = str(node.get("type") or "")
    if old == "video.montage":
        return "A montage of the photographs, held long enough to see each one."
    if old == "video.invitation":
        return "An invitation film, with every name, date and venue exactly as given."
    if old == "video.render":
        template = str(config.get("template") or "").replace("_", " ").strip()
        values = config.get("props")
        return (
            f"A {template or 'short'} video." + (f" Use these values: {values}" if values else "")
        ).strip()
    if old == "design.render":
        html = str(config.get("html") or "")[:4000]
        return f"Recreate this layout as a picture:\n{html}" if html else "A poster."
    if old == "image.edit":
        return "The photograph, cropped and corrected, at the size given."
    return name or "A short video."


def _rewrite(graph: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return graph, False

    changed = False
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            kept.append(node)
            continue
        old = str(node.get("type") or "")
        if old == "audio.speak":
            dropped.append(str(node.get("id") or ""))
            changed = True
            continue
        if old in VIDEO_TYPES or old in IMAGE_TYPES:
            config = dict(node.get("config") or {})
            carried = {
                "brief": _brief(node),
                "style": config.get("style", ""),
                "provider": config.get("provider", "anthropic"),
                "model": config.get("model", ""),
                "credential_id": config.get("credential_id", ""),
                "filename": config.get("filename", "video" if old in VIDEO_TYPES else "image"),
            }
            if old in VIDEO_TYPES:
                photos = config.get("photos") or config.get("images") or config.get("artifact_ids")
                node["type"] = "video.ai"
                node["config"] = {
                    **carried,
                    "photos": photos or "",
                    "duration_seconds": int(float(config.get("duration_seconds") or 8) or 8),
                    "size": config.get("size", "landscape"),
                    "narration": bool(config.get("narration", False)),
                    "voice": config.get("voice", "af_heart"),
                    "captions": bool(config.get("captions", True)),
                }
            else:
                node["type"] = "image.ai"
                node["config"] = {
                    **carried,
                    "photos": config.get("artifact_id", ""),
                    "size": config.get("size", "instagram_square"),
                    "width": int(config.get("width") or 0),
                    "height": int(config.get("height") or 0),
                }
            changed = True
        kept.append(node)

    if dropped:
        # Wire around each removed voice node, so the step after it still has
        # something before it. A node with nothing on either side just goes.
        for gone in dropped:
            sources = [e for e in edges if isinstance(e, dict) and e.get("target") == gone]
            targets = [e for e in edges if isinstance(e, dict) and e.get("source") == gone]
            for source in sources:
                for target in targets:
                    edges.append({"source": source["source"], "target": target["target"]})
        edges[:] = [
            edge
            for edge in edges
            if not (
                isinstance(edge, dict)
                and (edge.get("source") in dropped or edge.get("target") in dropped)
            )
        ]

    graph["nodes"] = kept
    graph["edges"] = edges
    return graph, changed


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, graph FROM flow_version")).fetchall()
    for row in rows:
        graph = row.graph
        if isinstance(graph, str):
            graph = json.loads(graph)
        if not isinstance(graph, dict):
            continue
        rewritten, changed = _rewrite(graph)
        if not changed:
            continue
        connection.execute(
            sa.text("UPDATE flow_version SET graph = :graph WHERE id = :id"),
            {"graph": json.dumps(rewritten), "id": row.id},
        )


def downgrade() -> None:
    """Nothing to do. The nodes this replaced no longer exist in the code, so
    putting their types back would produce graphs that cannot run."""
