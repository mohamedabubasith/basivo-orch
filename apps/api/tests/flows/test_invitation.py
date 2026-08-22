"""The invitation film.

Most of these exist because the same mistake was made twice while building it:
an element hidden by CSS while a *different* element was animated. Nothing
errors, nothing logs, the ornament simply never appears — and you only find out
by watching twenty-four seconds of video. So the composition is checked for the
property that was actually violated: whatever starts hidden is what the
timeline brings back.
"""

from __future__ import annotations

import re

import pytest

from basivo_orch.flows.nodes.invitation import (
    PALETTES,
    Function,
    InvitationConfig,
    build_invitation,
)


def an_invitation(**overrides) -> InvitationConfig:
    base = {
        "bride": "Meera",
        "groom": "Arjun",
        "date_line": "Sunday, 12 April 2026",
        "venue": "Sri Krishna Gardens, Coimbatore",
        "closing": "With love,\nthe Iyer and Rao families",
        "seconds": 24,
    }
    return InvitationConfig(**{**base, **overrides})


def compose(config: InvitationConfig, photos: list[str] | None = None) -> str:
    return build_invitation(
        config=config,
        photos=photos if photos is not None else ["couple0.jpg"],
        width=1080,
        height=1920,
        palette=PALETTES[config.palette],
        music=False,
    )


def hidden_ids(html: str) -> set[str]:
    """Ids the stylesheet hides AND the page actually contains.

    The stylesheet carries rules for scenes a given invitation may not have —
    a film with no function schedule still ships the rule for its heading. An
    unused rule is not a bug; an element on the page that never appears is.
    """
    styled = set(re.findall(r"#([a-z0-9]+) \{ opacity:0", html))
    body = html.split("</style>", 1)[-1]
    present = set(re.findall(r'id="([a-z0-9]+)"', body))
    return styled & present


def animated_ids(html: str) -> set[str]:
    return set(re.findall(r"tl\.(?:fromTo|to)\('#([a-z0-9]+)'", html))


def test_everything_hidden_is_brought_back():
    """The bug this suite exists for. Twice an ornament was hidden by one
    selector and animated by another, and the only symptom was an invitation
    with a blank space where the mandala should be."""
    html = compose(an_invitation(header_symbol="॥ शुभ विवाह ॥"))

    never_shown = hidden_ids(html) - animated_ids(html)
    assert not never_shown, f"hidden and never animated: {sorted(never_shown)}"


def test_the_corner_flourishes_are_animated_on_the_element_that_is_hidden():
    html = compose(an_invitation())
    assert ".c {" in html and "opacity:0" in html
    assert "tl.fromTo('.c'," in html, "the wrapper is hidden, so the wrapper must be revealed"


def test_a_name_in_tamil_or_devanagari_survives_intact():
    """The font stack is what stops these becoming empty boxes, and the stack
    only helps if the characters reach the page."""
    html = compose(an_invitation(bride="மீரா", groom="अर्जुन"))
    assert "மீரா" in html and "अर्जुन" in html
    assert "Noto Serif Tamil" in html and "Noto Serif Devanagari" in html


def test_a_line_break_is_a_line_break_and_markup_is_not():
    """An operator means two lines. A model, or a person, must not be able to
    mean anything else — this text arrives from a Telegram message."""
    html = compose(an_invitation(closing="With love,\nthe families"))
    assert "With love,<br>the families" in html

    hostile = compose(an_invitation(closing="<script>alert(1)</script>"))
    assert "<script>alert(1)</script>" not in hostile
    assert "&lt;script&gt;" in hostile


def test_the_photograph_is_not_hidden_behind_the_type():
    """A photography studio is selling the photograph. The first version washed
    it out under full-frame text, which is worse than having no picture."""
    html = compose(an_invitation())
    assert "with-photo" in html, "the type moves below the picture when there is one"
    # The middle of the frame, where the faces are, is left alone.
    assert "transparent 20%" in html and "transparent 46%" in html


def test_with_no_photographs_the_type_is_centred():
    html = compose(an_invitation(), photos=[])
    # The rule is always in the stylesheet; what matters is whether a scene
    # wears it.
    assert 'class="center with-photo"' not in html
    assert 'class="center "' in html or 'class="center"' in html


def test_each_photograph_leaves_when_its_scene_does():
    """Left showing, the last one sits under the schedule as a smudge."""
    html = compose(an_invitation(), photos=["couple0.jpg", "couple1.jpg"])
    assert "tl.to('#ph0', {opacity:0" in html
    assert "tl.to('#ph1', {opacity:0" in html


def test_the_schedule_is_staggered_not_dumped():
    config = an_invitation(
        functions=[
            Function(name="Mehendi", when="10 April"),
            Function(name="Sangeet", when="11 April"),
            Function(name="Muhurtham", when="12 April"),
        ]
    )
    html = compose(config)
    for index in range(3):
        assert f"'#fn{index}'" in html
    # Three different start times, so they arrive one after another.
    starts = re.findall(r"'#fn\d', \{opacity:0.*?\}, ([\d.]+)\);", html)
    assert len(set(starts)) == 3, f"all three arrive together: {starts}"


def test_the_film_fits_the_length_asked_for():
    for seconds in (10, 24, 45):
        html = compose(an_invitation(seconds=seconds))
        assert f'data-duration="{float(seconds):.3f}"' in html
        # Nothing is scheduled past the end.
        latest = max(float(value) for value in re.findall(r"\}, ([\d.]+)\);", html))
        assert latest <= seconds, f"{seconds}s film schedules something at {latest}s"


def test_the_renderer_will_accept_it():
    from basivo_orch.flows.nodes.video import composition_problems

    assert composition_problems(compose(an_invitation())) == []


def test_an_invitation_needs_a_name():
    from basivo_orch.flows.nodes.base import NodeError
    from basivo_orch.flows.nodes.invitation import InvitationNode

    node = InvitationNode()
    with pytest.raises(NodeError, match="at least one name"):
        import asyncio

        class Ctx:
            save_artifact = staticmethod(lambda *a, **k: None)

        asyncio.run(node.run(InvitationConfig(seconds=10), Ctx()))


def test_every_palette_is_a_complete_set():
    """A missing colour renders as the string 'undefined' in a gradient, which
    silently produces a black frame."""
    for name, palette in PALETTES.items():
        assert set(palette) == {"bg", "deep", "ink", "gold"}, name
        for key, value in palette.items():
            assert re.fullmatch(r"#[0-9a-f]{6}", value), f"{name}.{key} = {value!r}"
