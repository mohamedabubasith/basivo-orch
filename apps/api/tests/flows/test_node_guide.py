"""Every node explains itself: what it is for, what it needs, where it sits.

The palette's info button and the inspector read these. A node without them
is a node people add by guessing, which is how "why is my flow not starting"
questions happen.
"""

from __future__ import annotations

from basivo_orch.flows import nodes as registry


def test_every_node_has_a_guide() -> None:
    for node_type, cls in registry.REGISTRY.items():
        assert cls.when, f"{node_type} has no `when`"
        assert cls.needs, f"{node_type} has no `needs`"
        assert cls.example, f"{node_type} has no `example`"
        assert cls.description, f"{node_type} has no description"


def test_the_guide_reaches_the_palette() -> None:
    spec = registry.REGISTRY["git.autofix"].describe()
    assert spec["when"] and spec["example"]
    assert isinstance(spec["needs"], list) and spec["needs"]


def test_labels_are_words_not_identifiers() -> None:
    """The type is for machines; the label is what a person reads in the palette."""
    for node_type, cls in registry.REGISTRY.items():
        assert "." not in cls.label and "_" not in cls.label, f"{node_type}: {cls.label!r}"
        assert "—" not in cls.label + cls.description + cls.when + cls.example


def test_examples_name_real_nodes() -> None:
    """An example chain of labels that do not exist teaches the wrong thing."""
    labels = {cls.label for cls in registry.REGISTRY.values()}
    for node_type, cls in registry.REGISTRY.items():
        for step in cls.example.replace(", or", " ").split("->"):
            step = step.strip()
            assert step in labels, f"{node_type} example names {step!r}, not a node"


def test_non_trigger_nodes_say_what_comes_before_them() -> None:
    """The question behind "how is it started" is answered in `needs`."""
    feeders = ("trigger", "earlier node", "from the trigger", "photos", "text from", "number from")
    for node_type, cls in registry.REGISTRY.items():
        if cls.is_trigger:
            continue
        text = " ".join(cls.needs).lower()
        assert any(word in text for word in feeders), f"{node_type}: {cls.needs}"


def test_the_response_schema_carries_the_guide() -> None:
    """FastAPI's response_model filtering drops any field the schema does not
    list. This is the test that would have caught the blank guide dialog."""
    from basivo_orch.flows.schemas import NodeTypeRead

    for cls in registry.REGISTRY.values():
        read = NodeTypeRead.model_validate(cls.describe())
        assert read.when == cls.when and read.needs == list(cls.needs) and read.example == cls.example
