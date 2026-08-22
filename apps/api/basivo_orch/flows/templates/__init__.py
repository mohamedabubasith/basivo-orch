"""Flows that arrive already wired.

A studio owner should not have to assemble fourteen nodes to get a bot that
answers photographs. They should press one button, paste a token, and change
the wording. Everything here is an ordinary graph — nothing a template can do is
unavailable to someone drawing it by hand, which is the property that keeps a
template from becoming a second, worse product.

Each template names the credentials it needs, so the install screen can ask for
them instead of the first run failing with "pick a credential".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FlowTemplate:
    name: str
    title: str
    #: One line, shown on the card.
    summary: str
    #: What it is for and what it expects, shown once it is picked.
    detail: str
    #: Credential providers the flow cannot run without.
    needs: tuple[str, ...]
    build: Callable[..., dict[str, Any]]
    tags: tuple[str, ...] = field(default_factory=tuple)


def studio_video_bot(*, telegram_credential_id: str = "", llm_credential_id: str = "") -> dict:
    """A Telegram bot that turns photographs into a wedding invitation film.

    The shape of this graph is the answer to "how does a conversation fit in a
    DAG". It does not: each message is one short pass, and which pass depends on
    what arrived. So the whole flow is a router — is this a photograph, a
    request to make the film, a button press — and each branch is three or four
    nodes ending in a reply.

    Every branch replies. A bot that silently does nothing is indistinguishable
    from a bot that is broken, and the operator's next move when they think it
    is broken is to send everything again.
    """
    tg = telegram_credential_id
    return {
        "nodes": [
            {
                "id": "inbox",
                "type": "trigger.telegram",
                "name": "Telegram",
                "position": {"x": 40, "y": 320},
                "config": {"credential_id": tg, "download_media": True},
            },
            # --- is it a photograph? -------------------------------------
            {
                "id": "is_photo",
                "type": "logic.condition",
                "name": "A photo?",
                "position": {"x": 300, "y": 320},
                "config": {
                    "comparisons": [
                        {"left": "{{ input.kind }}", "operator": "equals", "right": "photo"}
                    ]
                },
            },
            {
                "id": "keep",
                "type": "session.state",
                "name": "Keep it",
                "position": {"x": 560, "y": 140},
                "config": {
                    "action": "add_photo",
                    "chat_id": "{{ input.chat_id }}",
                    "artifact_id": "{{ input.photos.0.artifact_id }}",
                    "file_unique_id": "{{ input.photos.0.file_unique_id }}",
                    "caption": "{{ input.text }}",
                },
            },
            {
                "id": "counted",
                "type": "telegram.reply",
                "name": "Say how many",
                "position": {"x": 820, "y": 140},
                "config": {
                    "credential_id": tg,
                    "action": "send",
                    "chat_id": "{{ input.chat_id }}",
                    "text": (
                        "{{ input.photo_count }} photo(s) saved. "
                        "Send more, or /make when you are ready."
                    ),
                    "silent": True,
                },
            },
            # --- is it a request to make the film? ------------------------
            {
                "id": "is_make",
                "type": "logic.condition",
                "name": "Make it?",
                "position": {"x": 560, "y": 420},
                "config": {
                    "match": "any",
                    "comparisons": [
                        {"left": "{{ input.command }}", "operator": "equals", "right": "/make"},
                        {
                            "left": "{{ input.callback_data }}",
                            "operator": "equals",
                            "right": "again",
                        },
                    ],
                },
            },
            {
                "id": "claim",
                "type": "session.state",
                "name": "One at a time",
                "position": {"x": 820, "y": 340},
                "config": {"action": "lock", "chat_id": "{{ input.chat_id }}"},
            },
            {
                "id": "free",
                "type": "logic.condition",
                "name": "Got the lock?",
                "position": {"x": 1080, "y": 340},
                "config": {
                    "comparisons": [
                        {"left": "{{ input.acquired }}", "operator": "equals", "right": True}
                    ]
                },
            },
            {
                "id": "busy",
                "type": "telegram.reply",
                "name": "Still working",
                "position": {"x": 1340, "y": 460},
                "config": {
                    "credential_id": tg,
                    "action": "send",
                    "chat_id": "{{ input.chat_id }}",
                    "text": "Still making the last one. I will send it here when it is done.",
                },
            },
            {
                "id": "starting",
                "type": "telegram.reply",
                "name": "Starting",
                "position": {"x": 1340, "y": 220},
                "config": {
                    "credential_id": tg,
                    "action": "send",
                    "chat_id": "{{ input.chat_id }}",
                    "text": "Making your invitation. About a minute.",
                },
            },
            {
                "id": "job",
                "type": "session.state",
                "name": "The job so far",
                "position": {"x": 1600, "y": 220},
                "config": {"action": "read", "chat_id": "{{ input.chat_id }}"},
            },
            # The director. Optional in spirit: if the model returns nonsense,
            # `merge_details` keeps the card it already had.
            {
                "id": "director",
                "type": "agent.llm",
                "name": "Read the brief",
                "position": {"x": 1860, "y": 220},
                "config": {
                    "credential_id": llm_credential_id,
                    "system": (
                        "You turn a photographer's note into the fields of a wedding "
                        "invitation card. Reply with JSON only, no prose, using these keys "
                        "when the note gives you them: bride, groom, joiner, date_line, "
                        "time_line, venue, closing, header_symbol, palette, functions "
                        "(a list of {name, when, where}). Leave out anything the note does "
                        "not mention rather than inventing it — a wrong date on an "
                        "invitation is worse than a missing one. palette must be one of "
                        "maroon_gold, ivory_gold, emerald_gold, blush_rose, royal_blue."
                    ),
                    "prompt": "{{ input.brief }}",
                    "max_iterations": 1,
                },
            },
            {
                "id": "film",
                "type": "video.invitation",
                "name": "The invitation",
                "position": {"x": 2120, "y": 220},
                "config": {
                    "photos": "{{ nodes.job.output.photo_ids }}",
                    "details": "{{ nodes.director.output.text }}",
                    "bride": "",
                    "groom": "",
                    "seconds": 22,
                    "aspect": "9:16",
                    "palette": "maroon_gold",
                    "quality": "standard",
                },
            },
            {
                "id": "deliver",
                "type": "telegram.reply",
                "name": "Send it",
                "position": {"x": 2380, "y": 220},
                "config": {
                    "credential_id": tg,
                    "action": "video",
                    "chat_id": "{{ nodes.job.output.chat_id }}",
                    "artifact_id": "{{ nodes.film.output.artifact_id }}",
                    "text": "Here it is. Happy with this one?",
                    "buttons": [
                        [
                            {"label": "Perfect", "data": "ok"},
                            {"label": "Try again", "data": "again"},
                        ]
                    ],
                },
            },
            {
                "id": "release",
                "type": "session.state",
                "name": "Release",
                "position": {"x": 2640, "y": 220},
                "config": {
                    "action": "update",
                    "chat_id": "{{ nodes.job.output.chat_id }}",
                    "state": "REVIEW",
                    "bump_iteration": True,
                    "last_video_artifact_id": "{{ nodes.film.output.artifact_id }}",
                },
            },
            {
                "id": "unlock",
                "type": "session.state",
                "name": "Unlock",
                "position": {"x": 2900, "y": 220},
                "config": {"action": "unlock", "chat_id": "{{ input.chat_id }}"},
            },
            # --- was it the approve button? -------------------------------
            {
                "id": "is_ok",
                "type": "logic.condition",
                "name": "Approved?",
                "position": {"x": 820, "y": 620},
                "config": {
                    "comparisons": [
                        {"left": "{{ input.callback_data }}", "operator": "equals", "right": "ok"}
                    ]
                },
            },
            {
                "id": "thanks",
                "type": "telegram.reply",
                "name": "Confirm",
                "position": {"x": 1080, "y": 560},
                "config": {
                    "credential_id": tg,
                    "action": "send",
                    "chat_id": "{{ input.chat_id }}",
                    "text": (
                        "Lovely. The video above is yours to forward. Send new photos "
                        "whenever you want another."
                    ),
                },
            },
            {
                "id": "help",
                "type": "telegram.reply",
                "name": "How it works",
                "position": {"x": 1080, "y": 720},
                "config": {
                    "credential_id": tg,
                    "action": "send",
                    "chat_id": "{{ input.chat_id }}",
                    "text": (
                        "Send me the couple's photos, then a note with the names, the "
                        "date, the venue and the functions. Then send /make and I will "
                        "put the invitation together."
                    ),
                },
            },
        ],
        "edges": [
            {"source": "inbox", "target": "is_photo"},
            {"source": "is_photo", "target": "keep", "source_handle": "true"},
            {"source": "keep", "target": "counted"},
            {"source": "is_photo", "target": "is_make", "source_handle": "false"},
            {"source": "is_make", "target": "claim", "source_handle": "true"},
            {"source": "claim", "target": "free"},
            {"source": "free", "target": "busy", "source_handle": "false"},
            {"source": "free", "target": "starting", "source_handle": "true"},
            {"source": "starting", "target": "job"},
            {"source": "job", "target": "director"},
            {"source": "director", "target": "film"},
            {"source": "film", "target": "deliver"},
            {"source": "deliver", "target": "release"},
            {"source": "release", "target": "unlock"},
            {"source": "is_make", "target": "is_ok", "source_handle": "false"},
            {"source": "is_ok", "target": "thanks", "source_handle": "true"},
            {"source": "is_ok", "target": "help", "source_handle": "false"},
        ],
    }


TEMPLATES: dict[str, FlowTemplate] = {
    "studio-video-bot": FlowTemplate(
        name="studio-video-bot",
        title="Studio video bot",
        summary="A Telegram bot that turns a couple's photos into an invitation film.",
        detail=(
            "Built for a photography studio. Send the bot photographs and a note with "
            "the names, date, venue and functions; it replies with a portrait video and "
            "a button to try again. Each message is its own run, so you can watch what "
            "it did and what it cost."
        ),
        needs=("telegram", "an LLM provider"),
        build=studio_video_bot,
        tags=("telegram", "video", "studio"),
    )
}
