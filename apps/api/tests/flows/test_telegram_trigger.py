"""Reading what Telegram actually sends.

The update object is a union of twenty optional keys and almost none of the
interesting cases look like the documentation's example. These are the shapes
that broke real bots: an album arriving as N separate updates, the same photo
sent from a desktop as a *document*, a button press whose "message" belongs to
the bot rather than the person, and a command addressed to a bot by name.

Kept as pure-function tests because that is where the surprises live. The
downloading half is exercised through the engine in test_engine_integration.
"""

from __future__ import annotations

from basivo_orch.flows.nodes.triggers import normalise_update

PHONE_PHOTO = {
    "update_id": 900001,
    "message": {
        "message_id": 42,
        "from": {"id": 7712, "first_name": "Ravi", "last_name": "K", "username": "ravi_studio"},
        "chat": {"id": 7712, "type": "private"},
        "caption": "the mandap shots",
        "media_group_id": "13548273812",
        "photo": [
            {"file_id": "small", "file_unique_id": "u1", "file_size": 1200},
            {"file_id": "medium", "file_unique_id": "u1", "file_size": 34000},
            {"file_id": "large", "file_unique_id": "u1", "file_size": 210000},
        ],
    },
}


def test_a_photo_from_a_phone():
    result = normalise_update(PHONE_PHOTO)

    assert result["kind"] == "photo"
    assert result["chat_id"] == "7712"
    assert result["user"]["name"] == "Ravi K"
    assert result["text"] == "the mandap shots"
    # The largest size, not the first: Telegram sends every thumbnail and the
    # first one is a postage stamp.
    assert result["files"][0]["file_id"] == "large"
    assert result["files"][0]["size"] == 210000


def test_an_album_is_many_updates_sharing_one_id():
    """Eight photos is eight updates milliseconds apart, not one update.

    Any design that treats an update as a complete instruction renders eight
    videos. The media_group_id is what lets a flow know these are one act.
    """
    first = normalise_update(PHONE_PHOTO)
    second = normalise_update(
        {
            **PHONE_PHOTO,
            "update_id": 900002,
            "message": {**PHONE_PHOTO["message"], "message_id": 43},
        }
    )
    assert first["media_group_id"] == second["media_group_id"] != ""
    assert first["update_id"] != second["update_id"]


def test_the_same_picture_sent_as_a_document():
    """Telegram Desktop's "send as file" keeps the original quality — which is
    what a photographer will do — and it arrives as a document, not a photo."""
    result = normalise_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 9,
                "from": {"id": 7712, "first_name": "Ravi"},
                "chat": {"id": 7712, "type": "private"},
                "document": {
                    "file_id": "doc1",
                    "file_unique_id": "u9",
                    "file_name": "DSC_4821.jpg",
                    "mime_type": "image/jpeg",
                    "file_size": 8_400_000,
                },
            },
        }
    )
    assert result["kind"] == "document"
    assert result["files"][0]["name"] == "DSC_4821.jpg"
    assert result["files"][0]["mime"] == "image/jpeg"


def test_a_button_press():
    """The message on a callback is the BOT's message. Reading `text` from it
    would give you your own last words back as though the operator said them."""
    result = normalise_update(
        {
            "update_id": 4,
            "callback_query": {
                "id": "cb-778",
                "from": {"id": 7712, "first_name": "Ravi"},
                "data": "approve:v3",
                "message": {
                    "message_id": 51,
                    "chat": {"id": 7712, "type": "private"},
                    "text": "Here is your video. Happy with it?",
                },
            },
        }
    )
    assert result["kind"] == "callback_query"
    assert result["callback_data"] == "approve:v3"
    assert result["callback_id"] == "cb-778"
    assert result["text"] == "", "the bot's own message is not something the operator said"
    assert result["chat_id"] == "7712"
    assert result["message_id"] == 51, "the message to edit in place"


def test_a_command_addressed_to_the_bot_by_name():
    """In a group, `/generate` is written `/generate@studio_bot`."""
    result = normalise_update(
        {
            "update_id": 5,
            "message": {
                "message_id": 12,
                "from": {"id": 1, "first_name": "Ravi"},
                "chat": {"id": -1001234567890, "type": "supergroup"},
                "text": "/generate@studio_bot cinematic, warm",
            },
        }
    )
    assert result["command"] == "/generate"
    assert result["text"] == "/generate@studio_bot cinematic, warm"
    assert result["chat_id"] == "-1001234567890", "group ids are negative"
    assert result["chat_type"] == "supergroup"


def test_an_update_with_no_chat_is_ignorable_not_fatal():
    """Channel posts, poll answers, chat-member changes. A bot in a busy group
    sees plenty of these, and each one failing a run makes the run list
    useless."""
    result = normalise_update({"update_id": 6, "poll_answer": {"poll_id": "1"}})
    assert result["chat_id"] == ""
    assert result["kind"] == "message"


def test_an_edited_message_is_still_read():
    """People fix typos in their prompt rather than sending it again."""
    result = normalise_update(
        {
            "update_id": 7,
            "edited_message": {
                "message_id": 12,
                "from": {"id": 1, "first_name": "Ravi"},
                "chat": {"id": 1, "type": "private"},
                "text": "make it 20 seconds not 30",
            },
        }
    )
    assert result["text"] == "make it 20 seconds not 30"
    assert result["chat_id"] == "1"


def test_a_voice_note_becomes_a_file():
    """A studio owner will describe what they want by talking, not typing."""
    result = normalise_update(
        {
            "update_id": 8,
            "message": {
                "message_id": 13,
                "from": {"id": 1, "first_name": "Ravi"},
                "chat": {"id": 1, "type": "private"},
                "voice": {
                    "file_id": "v1",
                    "file_unique_id": "uv",
                    "file_size": 40000,
                    "mime_type": "audio/ogg",
                },
            },
        }
    )
    assert result["kind"] == "audio"
    assert result["files"][0]["file_id"] == "v1"
