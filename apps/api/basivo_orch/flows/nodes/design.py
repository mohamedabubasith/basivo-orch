"""Rendering a poster from HTML, deterministically.

The obvious way to make a poster is to ask an image model for one. It is also
the wrong way: text-to-image models top out around ninety percent accuracy on
typography, which means one poster in ten has a typo in the customer's own
company name — unusable unattended, and the failure is silent.

So the model never draws the poster. It writes **HTML**, which a headless
browser renders at print scale with real fonts. The headline is the headline,
the brand colour is the brand colour, and running it twice produces the same
file. This is the approach HeyGen's HyperFrames takes for video, for the same
reason, and it costs nothing per render.

What a model is genuinely good at — layout, copy, taste in CSS — it does. What
it is bad at — drawing letterforms — it never touches.

**The page is treated as hostile.** HTML that reaches here may have been
written by a model working from an untrusted brief, so it renders in a browser
with JavaScript disabled and no access to the network beyond the fonts and
images explicitly allowed. Rendering is not a sandbox escape waiting to
happen just because the output is pretty.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

#: Presets, so nobody has to remember that a story is 1080×1920.
SIZES: dict[str, tuple[int, int]] = {
    "instagram_square": (1080, 1080),
    "instagram_portrait": (1080, 1350),
    "story": (1080, 1920),
    "twitter_landscape": (1600, 900),
    "linkedin": (1200, 627),
    "a4_portrait": (1240, 1754),
    "a4_landscape": (1754, 1240),
}

#: Hosts a rendered page may load fonts and images from. Anything else is
#: blocked at the browser, so a page cannot be used to probe the network it
#: renders in — the same rule the issue-screenshot fetcher follows.
ALLOWED_RESOURCE_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdn.jsdelivr.net",
    "unpkg.com",
    "images.unsplash.com",
)

RENDER_TIMEOUT_SECONDS = 45


class RenderConfig(BaseModel):
    model_config = {"extra": "forbid"}

    html: str = Field(
        min_length=1,
        max_length=400_000,
        description="The page to render. Supports {{ references }}, usually an agent's output.",
    )
    size: Literal[
        "instagram_square",
        "instagram_portrait",
        "story",
        "twitter_landscape",
        "linkedin",
        "a4_portrait",
        "a4_landscape",
        "custom",
    ] = "instagram_square"
    width: int = Field(default=0, ge=0, le=4000, description="Only used when size is custom.")
    height: int = Field(default=0, ge=0, le=4000, description="Only used when size is custom.")
    #: 2 is a retina render — the same layout at twice the pixels. Print wants
    #: 3; a Telegram photo does not.
    scale: int = Field(default=2, ge=1, le=4)
    format: Literal["png", "jpeg"] = "png"
    jpeg_quality: int = Field(default=90, ge=40, le=100)
    #: Web fonts arrive after first paint. Rendering before they land produces
    #: a poster in the fallback font, which looks fine and is wrong.
    wait_for_fonts: bool = True
    filename: str = Field(default="poster", max_length=100)

    @model_validator(mode="after")
    def _custom_needs_dimensions(self) -> RenderConfig:
        if self.size == "custom" and not (self.width and self.height):
            raise ValueError("A custom size needs both width and height.")
        return self

    def dimensions(self) -> tuple[int, int]:
        if self.size == "custom":
            return self.width, self.height
        return SIZES[self.size]


class RenderNode(Node):
    """HTML to an image file, at whatever size the channel wants."""

    type = "design.render"
    label = "Render Poster"
    description = (
        "Turn HTML into an image with real fonts, exact text and the same result every time."
    )
    tier = 2
    category = "design"
    config_model = RenderConfig
    output_paths = ("artifact_id", "url", "width", "height", "size_bytes")

    #: Headless Chromium with real fonts. Cheaper than a video, still the kind
    #: of work that should not run four-up on two cores.
    heavy: ClassVar[bool] = True
    max_attempts = 2
    timeout_seconds = 120.0

    async def run(self, config: RenderConfig, ctx: NodeContext) -> NodeResult:
        html = str(render_value(config.html, ctx.template_context()))
        width, height = config.dimensions()

        await ctx.step(
            "render.started",
            {"size": config.size, "width": width, "height": height, "scale": config.scale},
        )
        await ctx.progress(f"Rendering {width}×{height} at {config.scale}x")

        try:
            image = await asyncio.wait_for(
                _render(html, width=width, height=height, config=config),
                timeout=RENDER_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise NodeError(
                f"The page did not finish rendering within {RENDER_TIMEOUT_SECONDS}s. "
                "A web font or image that never loads is the usual cause."
            ) from exc

        saved = await ctx.save_artifact(
            image,
            filename=f"{config.filename}.{config.format}",
            content_type=f"image/{config.format}",
            node_id=ctx.node_id,
        )
        await ctx.step("render.finished", {**saved, "width": width, "height": height})

        return NodeResult(
            output={**saved, "width": width, "height": height},
        )


async def _render(html: str, *, width: int, height: int, config: RenderConfig) -> bytes:
    """Screenshot one page. Raises NodeError with something actionable."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise NodeError(
            "Rendering needs Playwright and a browser. Install it with "
            "`uv run playwright install chromium`."
        ) from exc

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                args=["--no-sandbox", "--font-render-hinting=none"]
            )
        except Exception as exc:  # noqa: BLE001 — the message matters more than the type
            raise NodeError(
                "Could not start the browser used for rendering. On a fresh install run "
                f"`uv run playwright install chromium`. ({exc})"
            ) from exc

        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=config.scale,
                # Deterministic output beats matching the viewer's locale: two
                # runs of the same flow must produce the same file.
                locale="en-GB",
                timezone_id="UTC",
                java_script_enabled=False,
            )
            page = await context.new_page()

            async def gate(route: Any) -> None:
                url = route.request.url
                if url.startswith("data:") or url.startswith("about:"):
                    await route.continue_()
                    return
                host = route.request.url.split("/")[2] if "//" in url else ""
                if any(host.endswith(allowed) for allowed in ALLOWED_RESOURCE_HOSTS):
                    await route.continue_()
                    return
                await route.abort()

            await page.route("**/*", gate)
            await page.set_content(html, wait_until="load")
            if config.wait_for_fonts:
                # Without this the screenshot can land before a web font does,
                # producing a poster in the fallback face — which looks fine,
                # and is not the design anyone approved.
                await page.evaluate("document.fonts && document.fonts.ready")

            shot: dict[str, Any] = {"type": config.format}
            if config.format == "jpeg":
                shot["quality"] = config.jpeg_quality
            return await page.screenshot(**shot)
        finally:
            await browser.close()
