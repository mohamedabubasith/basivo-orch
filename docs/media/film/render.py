"""The product film, rendered by this repository's own video node.

    uv run --directory apps/api python ../../docs/media/film/render.py render

Stills only (a contact sheet to check the layout) with no argument. The
screenshot assets live beside this file; the output lands in ~/Desktop.
"""
import asyncio, os, sys, time
sys.path.insert(0, "/Users/abu/OtherPythonProjects/basivo-orch/apps/api")
os.environ.setdefault("SECRET_KEY", "x"*48); os.environ.setdefault("ENVIRONMENT", "test")
from basivo_orch.flows.nodes.remotion import RenderJob, render, probe
from basivo_orch.flows.nodes.speech import speak
from basivo_orch.flows.nodes.video import caption_lines

OUT = os.path.expanduser("~/Desktop/basivo-videos")
HERE = os.path.dirname(os.path.abspath(__file__))
SCENE = open(f"{HERE}/scene.tsx").read()
NAMES = ("mark.png", "lt-run.png", "out-video.png", "out-montage.png", "out-quote.png")
ASSETS = {n: open(f"{HERE}/{n}", "rb").read() for n in NAMES}
A = 2560 / 1600

SHOTS = {
    "run": {"file": "lt-run.png", "aspect": A,
            "page": {"x": 0.33, "y": 0.06, "w": 0.44, "h": 0.30},
            "timeline": {"x": 0.3391, "y": 0.4305, "w": 0.425, "h": 0.1675},
            "autofix": {"x": 0.3668, "y": 0.5623, "w": 0.3594, "h": 0.0262}},
}
PR = {"number": 9, "title": "Autofix: Remove the old pricing script now that the service exists",
      "branch": "basivo/autofix-cf60f477", "added": 6, "removed": 23, "files": 4}
PROPS = {"tagline": "Draw the pipeline. It runs itself.", "url": "orch.basivo.in",
         "consoleUrl": "console.basivo.in", "pr": PR, "shots": SHOTS, "voiceOver": False}
SECONDS = 63.2

SCRIPT = (
    "Basivo. Draw the pipeline, and it runs itself. "
    "It starts however your work starts: an issue, a ticket, your own webhook, a timetable, "
    "a message, or you pressing run. Nothing to wire up by hand. "
    "Then you draw the flow on one canvas, from eighteen node types. "
    "The agent is a real agent: tools, skills, sub-agents, MCP servers, hand over. "
    "Bring your own key, or use the free one built in. "
    "It makes more than text. React goes in, an MP4 comes out: renders, montages, a voice over the top. "
    "And it builds applications. Describe the page you want, and watch it appear beside what "
    "you typed. Change your mind, and it changes. Every build is a version. "
    "Deploy, and you have an address to send to anyone. The code is yours whenever you want it. "
    "Then it delivers: a pull request, a comment, a post where your people are. "
    "Every node reports itself while the run is going. One real run: thirty six seconds, "
    "nine cents. "
    "Elsewhere that is a webhook you wire by hand and a render box beside the automation tool. "
    "Here it is one canvas, and the cost is on the run. "
    "Basivo. Orch dot basivo dot in."
)

def job(seconds, props=None, **kw):
    return RenderJob(scene_tsx=SCENE, width=1920, height=1080, fps=30,
                     duration_seconds=seconds, props=props or PROPS,
                     background="#eef1f7", quality="high", assets=ASSETS, **kw)

async def sheet():
    frames = [int(SECONDS * 30 * p / 100) for p in (3, 12, 23, 34, 43, 54, 58, 65, 73, 81, 88, 97)]
    stills = await probe(job(SECONDS), frames=frames, scale=0.5)
    for name, data in zip(frames, stills):
        open(f"{HERE}/p{name}.png", "wb").write(data)
    print("probed", frames)

async def main():
    started = time.time()
    audio, seconds, words = await speak(SCRIPT, voice="bf_emma", speed=1.0)
    print(f"voice: {seconds:.1f}s over {len(words)} words")
    open(f"{OUT}/15-film-narration.wav", "wb").write(audio)

    data, _ = await render(job(SECONDS))
    open(f"{OUT}/15-film-silent.mp4", "wb").write(data)
    print(f"silent: {len(data)//1024} KB in {time.time()-started:.0f}s")

    narrated = job(round(seconds + 1.0, 1), props={**PROPS, "voiceOver": True})
    narrated = RenderJob(scene_tsx=SCENE, width=1920, height=1080, fps=30,
                         duration_seconds=round(seconds + 1.0, 1),
                         props={**PROPS, "voiceOver": True}, background="#eef1f7",
                         quality="high", assets={**ASSETS, "narration.wav": audio},
                         audio="narration.wav", captions=caption_lines(words))
    data, _ = await render(narrated)
    open(f"{OUT}/16-film-voice.mp4", "wb").write(data)
    print(f"narrated: {len(data)//1024} KB, {round(seconds + 1.0, 1)}s")

    still = await probe(job(SECONDS), frames=[int(SECONDS * 30 * 0.27)], scale=1.0)
    open(f"{OUT}/15-film-poster.png", "wb").write(still[0])

asyncio.run(main() if "render" in sys.argv else sheet())
