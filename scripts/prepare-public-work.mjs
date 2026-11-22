import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { parseArgs } from "node:util";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const { values } = parseArgs({
  options: {
    playwright: { type: "string" },
    url: { type: "string" },
    guide: { type: "string" },
    out: { type: "string" },
    synthetic: { type: "boolean", default: false },
  },
});
for (const name of ["playwright", "url", "guide", "out"])
  assert.ok(values[name], `Pass --${name} explicitly`);
const source = new URL(values.url);
assert.ok(["http:", "https:"].includes(source.protocol));
assert.equal(source.username + source.password + source.search + source.hash, "");
const directory = resolve(values.out);
const guidePath = resolve(values.guide);
const playwright = resolve(values.playwright);
const guideSource = await readFile(guidePath);
const coverName = "cover.png";
const guideName = "visitor-guide.pdf";
await mkdir(directory, { mode: 0o700 });

const { chromium } = await import(pathToFileURL(playwright).href);
const browser = await chromium.launch({
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
    locale: "zh-CN",
  });
  assert.deepEqual(await context.cookies(), []);
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const writes = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD"].includes(request.method()))
      writes.push(request.method());
  });
  const response = await page.goto(source.href, {
    waitUntil: "networkidle",
  });
  assert.equal(response.status(), 200);
  await page.evaluate(() => document.fonts.ready.then(() => true));
  assert.equal(
    (await page.locator("body").innerText()).includes("退出登录"),
    false,
  );
  assert.deepEqual(writes, []);
  assert.deepEqual(await context.cookies(), []);
  const cover = await page.screenshot({
    fullPage: true,
    animations: "disabled",
  });
  const capturedAt = new Date().toISOString();
  const width = cover.readUInt32BE(16);
  const height = cover.readUInt32BE(20);
  assert.ok(cover.length <= 8 * 1024 * 1024 && width * height <= 16000000);

  const guide = await context.newPage();
  await guide.goto(pathToFileURL(guidePath).href);
  await guide.evaluate(() => document.fonts.ready.then(() => true));
  const pdf = await guide.pdf({
    format: "A4",
    printBackground: true,
    preferCSSPageSize: true,
  });
  const compiledAt = new Date().toISOString();
  assert.equal(pdf.subarray(0, 5).toString(), "%PDF-");
  const pages = pdf
    .toString("latin1")
    .match(/\/Type \/Pages\b[\s\S]*?\/Count (\d+)/);
  assert.equal(pages?.[1], "1", "The visitor guide must fit one complete page");
  await writeFile(resolve(directory, coverName), cover, { flag: "wx" });
  await writeFile(resolve(directory, guideName), pdf, { flag: "wx" });
  const digest = (data) => createHash("sha256").update(data).digest("hex");
  const record = {
    version: 1,
    synthetic: values.synthetic,
    screenshot: {
      filename: coverName,
      source_url: source.href,
      captured_at: capturedAt,
      authenticated: false,
      width,
      height,
      size_bytes: cover.length,
      sha256: digest(cover),
    },
    document: {
      filename: guideName,
      source: "explicit-local-html",
      source_sha256: digest(guideSource),
      compiled_at: compiledAt,
      newly_written: true,
      version_date: compiledAt.slice(0, 10),
      size_bytes: pdf.length,
      sha256: digest(pdf),
    },
  };
  await writeFile(
    resolve(directory, "sources.json"),
    `${JSON.stringify(record, null, 2)}\n`,
    {
      flag: "wx",
    },
  );
  console.log(JSON.stringify(record));
} finally {
  await browser.close();
}
