/*
 * Tests for adapters.js, with no browser and no dependencies.
 *
 *   node extension/tests/adapters.test.js
 *
 * The extension half of this project is the part hardest to verify: loading it
 * in Chrome and clicking through a real page is not something a test can do. But
 * almost all of the logic that actually breaks is pure string work on a URL and
 * a title, and that needs nothing but a stub for `document` and `location`.
 *
 * The URL shapes below are real, taken from live sites.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const SRC = fs.readFileSync(
  path.join(__dirname, "..", "src", "adapters.js"), "utf8");

/** Minimal document stub. `els` maps an exact selector to its text. */
function makeDocument({ title = "", metas = {}, els = {}, images = [] } = {}) {
  return {
    title,
    images,
    querySelector(sel) {
      // Selectors arrive comma-separated ("a, b, c"), which is how detect()
      // asks about several candidate markups at once. Each part is tried in
      // turn, the way a browser would.
      for (const part of String(sel).split(",").map((s) => s.trim())) {
        if (!part) continue;
        const meta = part.match(/^meta\[(?:property|name)=["']([^"']+)["']\]$/);
        if (meta) {
          const v = metas[meta[1]];
          if (v) return { getAttribute: () => v, textContent: v };
          continue;
        }
        if (Object.prototype.hasOwnProperty.call(els, part)) {
          const v = els[part];
          return { getAttribute: () => v, textContent: v };
        }
      }
      return null;
    },
    querySelectorAll: () => [],
  };
}

function makeLocation(href) {
  const u = new URL(href);
  return { href, pathname: u.pathname, hostname: u.hostname, search: u.search };
}

/** Load adapters.js fresh against a given stubbed environment. */
function load(doc, loc) {
  delete globalThis.NW_ADAPTERS;
  const fn = new Function("document", "location",
    SRC + "\nreturn NW_ADAPTERS;");
  return fn(doc, loc);
}

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed++;
    console.log(`ok   ${name}`);
  } catch (e) {
    failures.push(name);
    console.log(`FAIL ${name}`);
    console.log(`       ${e.message}`);
  }
}

// A bare environment is enough for the pure helpers.
const A = load(makeDocument(), makeLocation("https://example.com/"));

console.log("=== season and episode out of a URL ===");

const URL_CASES = [
  // the movies2watch family: /series/<slug>-<id>/<season>-<episode>/
  ["https://movies2watch.vc/series/the-mentalist-12123/1-8/", 1, 8],
  ["https://movies2watch.vc/series/severance-99881/2-7", 2, 7],
  ["https://movies2watch.vc/series/the-wire-40122/4-13/?x=1", 4, 13],
  // other shapes that already worked, and must keep working
  ["https://site.tv/watch/show/season/5/episode/14", 5, 14],
  ["https://site.tv/breaking-bad-season-5-episode-14", 5, 14],
  ["https://site.tv/x/s05e14", 5, 14],
  ["https://site.tv/x/3x10", 3, 10],
];
for (const [href, season, episode] of URL_CASES) {
  check(`${href.replace(/^https:\/\//, "")}`, () => {
    const se = A.findSeasonEpisode([href]);
    assert.strictEqual(se.season, season, `season: got ${se.season}`);
    assert.strictEqual(se.episode, episode, `episode: got ${se.episode}`);
  });
}

console.log("\n=== URL shapes that must NOT be read as season/episode ===");
for (const href of [
  "https://movies2watch.vc/movie/blade-runner-2049-19827/",
  "https://site.tv/archive/2024-08/something",
  "https://site.tv/movie/the-thing-1982",
]) {
  check(`${href.replace(/^https:\/\//, "")} has no S/E`, () => {
    const se = A.findSeasonEpisode([href]);
    assert.ok(!se.season && !se.episode,
      `got season=${se.season} episode=${se.episode}`);
  });
}

console.log("\n=== show name out of a URL slug ===");
for (const [pathname, want] of [
  ["/series/the-mentalist-12123/1-8/", "The Mentalist"],
  ["/series/severance-99881/2-7", "Severance"],
  ["/movie/blade-runner-2049-19827/", "Blade Runner 2049"],
  ["/", ""],
]) {
  check(`slug ${pathname}`, () => {
    assert.strictEqual(A.slugTitle(pathname), want);
  });
}

console.log("\n=== title cleaning ===");
for (const [raw, want] of [
  // The bracketed year comes out too: it is published as its own field, so
  // leaving it in reads as though it were part of the name.
  ["Watch The Mentalist (2008) Online free on Movies2Watch", "The Mentalist"],
  ["Breaking Bad | movies2watch.tv", "Breaking Bad"],
  ["Girl on Fire", "Girl on Fire"],
]) {
  check(`clean ${JSON.stringify(raw.slice(0, 40))}`, () => {
    assert.strictEqual(A.cleanTitle(raw), want);
  });
}

console.log("\n=== the generic adapter on a real movies2watch URL ===");
check("reads season, episode and title", () => {
  const doc = makeDocument({
    title: "Watch The Mentalist (2008) Online free on Movies2Watch",
    metas: {
      "og:title": "The Mentalist",
      "og:image": "https://img.example.com/poster.jpg",
      "og:type": "video.tv_show",
    },
  });
  const loc = makeLocation(
    "https://movies2watch.vc/series/the-mentalist-12123/1-8/");
  const api = load(doc, loc);
  const out = api.read();
  assert.strictEqual(out.title, "The Mentalist", `title: ${out.title}`);
  assert.strictEqual(out.kind, "series", `kind: ${out.kind}`);
  assert.strictEqual(out.season, 1, `season: ${out.season}`);
  assert.strictEqual(out.episode, 8, `episode: ${out.episode}`);
  assert.strictEqual(out.poster, "https://img.example.com/poster.jpg");
  assert.strictEqual(out.adapter, "generic");
});

console.log("\n=== falls back to the slug when the page offers no usable title ===");
check("slug fallback", () => {
  const doc = makeDocument({ title: "" });
  const loc = makeLocation(
    "https://movies2watch.vc/series/the-mentalist-12123/1-8/");
  const api = load(doc, loc);
  const out = api.read();
  assert.strictEqual(out.title, "The Mentalist", `title: ${out.title}`);
  assert.strictEqual(out.season, 1);
  assert.strictEqual(out.episode, 8);
});

console.log("\n=== the sflix-family adapter wins when its markup is present ===");
check("detects and reads the template", () => {
  const doc = makeDocument({
    title: "Watch The Mentalist (2008) Online free on Movies2Watch",
    metas: { "og:title": "The Mentalist" },
    els: {
      "#detail-container": "x",
      ".heading-name a": "The Mentalist",
      ".ss-list a.ep-item.active": "Eps 8: Great Red Dragon",
    },
  });
  const loc = makeLocation(
    "https://movies2watch.vc/series/the-mentalist-12123/1-8/");
  const api = load(doc, loc);
  const picked = api.pick();
  assert.strictEqual(picked.id, "sflix-family", `picked ${picked.id}`);
  const out = api.read();
  assert.strictEqual(out.title, "The Mentalist", `title: ${out.title}`);
  assert.strictEqual(out.season, 1, `season: ${out.season}`);
  assert.strictEqual(out.episode, 8, `episode: ${out.episode}`);
  assert.strictEqual(out.episode_title, "Great Red Dragon",
    `episode_title: ${out.episode_title}`);
});

console.log(
  `\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
