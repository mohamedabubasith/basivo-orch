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


def studio_video_bot(
    *,
    telegram_credential_id: str = "",
    llm_credential_id: str = "",
    llm_provider: str = "anthropic",
    llm_model: str = "",
) -> dict:
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
                    "chat_id": "{{ nodes.inbox.output.chat_id }}",
                    "artifact_id": "{{ nodes.inbox.output.photos.0.artifact_id }}",
                    "file_unique_id": "{{ nodes.inbox.output.photos.0.file_unique_id }}",
                    "caption": "{{ nodes.inbox.output.text }}",
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
                    "chat_id": "{{ nodes.inbox.output.chat_id }}",
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
                        {
                            "left": "{{ nodes.inbox.output.command }}",
                            "operator": "equals",
                            "right": "/make",
                        },
                        {
                            "left": "{{ nodes.inbox.output.callback_data }}",
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
                "config": {"action": "lock", "chat_id": "{{ nodes.inbox.output.chat_id }}"},
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
                    "chat_id": "{{ nodes.inbox.output.chat_id }}",
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
                    "chat_id": "{{ nodes.inbox.output.chat_id }}",
                    "text": "Making your invitation. About a minute.",
                },
            },
            {
                "id": "job",
                "type": "session.state",
                "name": "The job so far",
                "position": {"x": 1600, "y": 220},
                "config": {"action": "read", "chat_id": "{{ nodes.inbox.output.chat_id }}"},
            },
            {
                "id": "director",
                "type": "agent.llm",
                "name": "Read the brief",
                "position": {"x": 1860, "y": 220},
                "config": {
                    "credential_id": llm_credential_id,
                    # Taken from the credential at install time. A node saying
                    # "anthropic" holding an OpenAI key calls the wrong
                    # vendor's endpoint with the right secret, and the error
                    # that comes back explains none of that.
                    "provider": llm_provider,
                    "model": llm_model,
                    "system": (
                        "You turn a photographer's rough note into a brief for whoever "
                        "designs the video. Write plain prose, no JSON, no preamble.\n\n"
                        "Say what the video is for (a wedding invitation, an engagement "
                        "announcement, a birthday), then every fact the note gives you, "
                        "spelled exactly as written: names, dates, times, venues, the "
                        "order of functions. Never invent, correct or complete a fact — a "
                        "wrong date on an invitation is worse than a missing one, and you "
                        "cannot tell which detail the family will check first.\n\n"
                        "Then describe how it should look and feel in two or three "
                        "sentences: colours, the mood, how the photographs should be used, "
                        "what should be on screen at the start and at the end. Where the "
                        "note says nothing, choose something appropriate to the occasion "
                        "and the culture it plainly belongs to, and say so plainly rather "
                        "than hedging."
                    ),
                    "prompt": "{{ input.brief }}",
                    "max_iterations": 1,
                },
            },
            {
                "id": "film",
                "type": "video.generate",
                "name": "Make the video",
                "position": {"x": 2120, "y": 220},
                "config": {
                    "credential_id": llm_credential_id,
                    "provider": llm_provider,
                    "model": llm_model,
                    # What the operator typed, put in front of a model that
                    # writes the animation. This is the path a studio can take
                    # anywhere — an engagement film, a house-warming, a temple
                    # function — without anyone adding a node for each one.
                    "brief": "{{ nodes.director.output.text }}",
                    "photos": "{{ nodes.job.output.photo_ids }}",
                    "duration_seconds": 20,
                    "size": "story",
                    "quality": "standard",
                    "fps": 30,
                    "max_attempts": 3,
                    "filename": "video",
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
                "config": {"action": "unlock", "chat_id": "{{ nodes.inbox.output.chat_id }}"},
            },
            # --- was it the approve button? -------------------------------
            {
                "id": "is_ok",
                "type": "logic.condition",
                "name": "Approved?",
                "position": {"x": 820, "y": 620},
                "config": {
                    "comparisons": [
                        {
                            "left": "{{ nodes.inbox.output.callback_data }}",
                            "operator": "equals",
                            "right": "ok",
                        }
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
                    "chat_id": "{{ nodes.inbox.output.chat_id }}",
                    "text": (
                        "Lovely. The video above is yours to forward. Send new photos "
                        "whenever you want another."
                    ),
                },
            },
            {
                "id": "is_note",
                "type": "logic.condition",
                "name": "A note?",
                "position": {"x": 1080, "y": 720},
                "config": {
                    "match": "all",
                    "comparisons": [
                        {
                            "left": "{{ nodes.inbox.output.text }}",
                            "operator": "is_not_empty",
                        },
                        {"left": "{{ nodes.inbox.output.command }}", "operator": "is_empty"},
                    ],
                },
            },
            {
                "id": "note",
                "type": "session.state",
                "name": "Remember the brief",
                "position": {"x": 1340, "y": 660},
                "config": {
                    "action": "update",
                    "chat_id": "{{ nodes.inbox.output.chat_id }}",
                    "brief": "{{ nodes.inbox.output.text }}",
                },
            },
            {
                "id": "noted",
                "type": "telegram.reply",
                "name": "Confirm the brief",
                "position": {"x": 1600, "y": 660},
                "config": {
                    "credential_id": tg,
                    "action": "send",
                    "chat_id": "{{ input.chat_id }}",
                    "text": "Noted. Send /make when you are ready and I will put it together.",
                    "silent": True,
                },
            },
            {
                "id": "help",
                "type": "telegram.reply",
                "name": "How it works",
                "position": {"x": 1340, "y": 860},
                "config": {
                    "credential_id": tg,
                    "action": "send",
                    "chat_id": "{{ nodes.inbox.output.chat_id }}",
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
            {"source": "is_ok", "target": "is_note", "source_handle": "false"},
            {"source": "is_note", "target": "note", "source_handle": "true"},
            {"source": "note", "target": "noted"},
            {"source": "is_note", "target": "help", "source_handle": "false"},
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
