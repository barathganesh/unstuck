/**
 * Render every slide of unstuck-pitch.html to an exact 1280 x 720 PNG.
 *
 * Deterministic and local only: it loads the deck from the file system with
 * a headless browser, sets the viewport to the frame size so 1 cqw resolves
 * to 12.8 px exactly as designed, disables animation, and screenshots the
 * stage element. No network request is made and nothing is uploaded.
 */
import { mkdir, readdir, unlink } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import puppeteer from "puppeteer";

const HERE = dirname(fileURLToPath(import.meta.url));
const PITCH = resolve(HERE, "..");
const DECK = join(PITCH, "unstuck-pitch.html");
const FRAMES = join(PITCH, "frames");

const WIDTH = 1280;
const HEIGHT = 720;
const COUNT = 8;

const keepMotion = process.argv.includes("--motion");

await mkdir(FRAMES, { recursive: true });
for (const name of await readdir(FRAMES)) {
  if (/^slide-\d\d\.png$/.test(name)) await unlink(join(FRAMES, name));
}

const browser = await puppeteer.launch({
  headless: "shell",
  args: ["--force-device-scale-factor=1", "--hide-scrollbars"],
});

try {
  const page = await browser.newPage();
  await page.setViewport({ width: WIDTH, height: HEIGHT, deviceScaleFactor: 1 });
  await page.setOfflineMode(false);

  const requests = [];
  page.on("request", (r) => {
    const url = r.url();
    if (!url.startsWith("file:") && !url.startsWith("data:")) requests.push(url);
  });
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });

  await page.goto(pathToFileURL(DECK).href, { waitUntil: "load" });

  if (!keepMotion) {
    await page.addStyleTag({
      content: "*,*::before,*::after{animation:none!important;transition:none!important}",
    });
  }

  const found = await page.$$eval(".slide", (n) => n.length);
  if (found !== COUNT) throw new Error(`expected ${COUNT} slides, found ${found}`);

  for (let i = 1; i <= COUNT; i += 1) {
    await page.evaluate((n) => {
      document.querySelectorAll(".slide").forEach((s, k) => {
        s.hidden = k !== n - 1;
        s.classList.toggle("is-live", k === n - 1);
      });
      document.getElementById("controls").style.display = "none";
      document.getElementById("progress").style.display = "none";
    }, i);
    await new Promise((r) => setTimeout(r, 120));
    const stage = await page.$("#deck");
    const file = join(FRAMES, `slide-${String(i).padStart(2, "0")}.png`);
    await stage.screenshot({ path: file, captureBeyondViewport: false });
    process.stdout.write(`wrote frames/slide-${String(i).padStart(2, "0")}.png\n`);
  }

  if (requests.length) {
    throw new Error(`deck attempted ${requests.length} non-local request(s): ${requests.join(", ")}`);
  }
  if (consoleErrors.length) {
    throw new Error(`deck reported script errors: ${consoleErrors.join(" | ")}`);
  }
  process.stdout.write("no external request, no script error\n");
} finally {
  await browser.close();
}
