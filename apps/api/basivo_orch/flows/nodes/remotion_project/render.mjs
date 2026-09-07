/**
 * One process: bundle once, look at a few frames, then encode.
 *
 * Doing those as separate commands would bundle three times and launch three
 * browsers, which on a worker that also runs everything else is minutes of
 * pure waste. More importantly it would make the check meaningless: the
 * frames you inspect have to come from the same bundle you are about to
 * render, or you are approving one video and shipping another.
 *
 * Everything it needs arrives as one JSON file on argv, and everything it has
 * to say leaves as one JSON line on stdout prefixed with RESULT. Remotion's
 * own progress output goes to stderr and is kept for the error message,
 * because "the render failed" without the renderer's last words is not a
 * report anybody can act on.
 */
import { readFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { bundle } from "@remotion/bundler";
import {
  ensureBrowser,
  getCompositions,
  openBrowser,
  renderMedia,
  renderStill,
} from "@remotion/renderer";

const jobPath = process.argv[2];
if (!jobPath) {
  console.error("usage: node render.mjs <job.json>");
  process.exit(2);
}

const job = JSON.parse(readFileSync(jobPath, "utf8"));
const say = (payload) => console.log("RESULT " + JSON.stringify(payload));

const main = async () => {
  // Downloads Chrome Headless Shell if the image did not bake it in. On a
  // worker this is a no-op; on a laptop it is the difference between a
  // confusing failure and a slow first run.
  await ensureBrowser();

  const entry = join(job.projectRoot, "src", "index.ts");
  const bundled = await bundle({
    entryPoint: entry,
    publicDir: join(job.projectRoot, "public"),
    // Silent unless something is wrong: a progress bar written to a pipe is
    // just noise in the log we keep for failures.
    onProgress: () => undefined,
    webpackOverride: (config) => config,
  });

  const compositions = await getCompositions(bundled);
  const composition = compositions.find((item) => item.id === "Main");
  if (!composition) {
    throw new Error("The bundle has no composition called Main.");
  }

  // One browser for the stills and the render. Each launch is about a second
  // and a quarter of a gigabyte.
  const browser = await openBrowser("chrome", {
    chromiumOptions: { gl: job.gl || "swangle" },
  });

  try {
    const probes = [];
    if (Array.isArray(job.probeFrames) && job.probeFrames.length > 0) {
      const probeDir = join(job.workDir, "probe");
      mkdirSync(probeDir, { recursive: true });
      for (const frame of job.probeFrames) {
        const safe = Math.max(0, Math.min(composition.durationInFrames - 1, Math.round(frame)));
        const output = join(probeDir, `frame-${safe}.png`);
        await renderStill({
          composition,
          serveUrl: bundled,
          output,
          frame: safe,
          puppeteerInstance: browser,
          // A still is only being looked at by a histogram, so it is rendered
          // small. Full size would cost seconds each and change no verdict.
          scale: job.probeScale ?? 0.25,
          imageFormat: "png",
        });
        probes.push({ frame: safe, path: output });
      }
    }

    if (job.probeOnly) {
      say({ ok: true, probes, durationInFrames: composition.durationInFrames, fps: composition.fps });
      return;
    }

    await renderMedia({
      composition,
      serveUrl: bundled,
      codec: job.codec || "h264",
      outputLocation: job.output,
      puppeteerInstance: browser,
      concurrency: job.concurrency || null,
      crf: job.crf ?? undefined,
      jpegQuality: job.jpegQuality ?? undefined,
      // Audio comes from the composition itself (the narration sibling), so
      // there is no second muxing path to keep in step with this one.
      enforceAudioTrack: false,
      onProgress: () => undefined,
    });

    say({
      ok: true,
      probes,
      output: job.output,
      durationInFrames: composition.durationInFrames,
      fps: composition.fps,
      width: composition.width,
      height: composition.height,
    });
  } finally {
    await browser.close({ silent: true }).catch(() => undefined);
  }
};

main().catch((error) => {
  // The message is what the author sees when their composition does not
  // compile, so it carries the whole thing rather than a summary.
  say({ ok: false, error: String(error && error.stack ? error.stack : error) });
  process.exit(1);
});
