/*
 * Service worker. Three jobs:
 *
 *   1. Own the enabled-site list, and register content scripts at RUNTIME.
 *      Nothing is declared in the manifest, so the extension installs asking
 *      for no site access at all and the user grants one domain at a time.
 *      This is what makes mirror #47 work without a store update.
 *
 *   2. Merge the two halves of a report. The top frame knows the title; the
 *      cross-origin player iframe knows the playback clock. Neither can see
 *      the other, so they report separately and get joined here by tabId.
 *
 *   3. Decide which tab wins when several are open, and POST to the local
 *      bridge.
 *
 * Note this worker is allowed to die. Nothing here is durable state: every
 * content script re-reports every couple of seconds, so `live` rebuilds within
 * one tick of a wake-up, and the bridge independently clears a presence whose
 * reports have gone stale. That belt-and-braces pairing is deliberate. If the
 * browser is killed outright, nobody sends a clear and only the bridge's
 * staleness timer saves you.
 */

const SCRIPT_ID = "nw-collector";
const JS = ["src/adapters.js", "src/collector.js"];
const DEFAULTS = { sites: [], probeFrames: false, port: 6788 };

// Treat a playback snapshot older than this as absent: the player iframe went
// away, or the tab was backgrounded hard enough to stop the timer.
const VIDEO_TTL_MS = 8000;

// Re-POST unchanged state this often so the bridge's staleness timer stays fed.
const HEARTBEAT_MS = 5000;

/** tabId -> {meta, site, url, hasPlayer, metaAt, video, videoAt} */
const live = new Map();

let lastSig = null;
let lastPostAt = 0;
let bridgeOk = false;
let publishing = false;

// ---- state -----------------------------------------------------------------

async function getState() {
  const s = await chrome.storage.local.get(DEFAULTS);
  return {
    sites: Array.isArray(s.sites) ? s.sites : [],
    probeFrames: !!s.probeFrames,
    port: Number(s.port) || DEFAULTS.port,
  };
}

/** Bare hostname, no scheme and no www, which is the form we store. */
function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
}

/**
 * Both patterns are needed: "*://site.tv/*" alone does not match
 * "www.site.tv", and these sites redirect between the two freely.
 */
function patternsFor(host) {
  return [`*://${host}/*`, `*://*.${host}/*`];
}

function hostEnabled(host, sites) {
  if (!host) return false;
  return sites.some((s) => host === s || host.endsWith(`.${s}`));
}

// ---- dynamic content scripts ----------------------------------------------

async function syncScripts() {
  const state = await getState();
  const matches = state.sites.flatMap(patternsFor);
  if (state.probeFrames) matches.push("<all_urls>");

  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({
      ids: [SCRIPT_ID],
    });
    if (existing.length) {
      await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] });
    }
    if (!matches.length) return { ok: true };

    await chrome.scripting.registerContentScripts([
      {
        id: SCRIPT_ID,
        matches,
        js: JS,
        allFrames: true,
        runAt: "document_idle",
        persistAcrossSessions: true,
      },
    ]);
    return { ok: true };
  } catch (e) {
    // The usual cause is a match pattern whose permission was revoked from
    // chrome://extensions behind our back. Surfaced in the popup rather than
    // swallowed, because the symptom otherwise is "nothing happens".
    console.warn("nowwatching: registerContentScripts failed", e);
    return { ok: false, error: String(e) };
  }
}

// ---- bridge ---------------------------------------------------------------

async function bridge(path, body) {
  const { port } = await getState();
  const url = `http://127.0.0.1:${port}${path}`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    bridgeOk = res.ok;
    return res.ok ? await res.json() : null;
  } catch {
    // Almost always "the daemon is not running", which is a normal state and
    // not worth a console error on every tick.
    bridgeOk = false;
    return null;
  }
}

function renderBadge() {
  const text = publishing ? (bridgeOk ? "●" : "!") : "";
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({
    color: bridgeOk ? "#5865F2" : "#b45309",
  });
}

// ---- merge and publish ----------------------------------------------------

function freshVideo(entry) {
  if (!entry.video) return null;
  return Date.now() - entry.videoAt < VIDEO_TTL_MS ? entry.video : null;
}

/**
 * Which tab is the user actually watching?
 *
 * A tab with a running video always beats one that is merely open, so an
 * abandoned detail page in another tab cannot hijack the presence. Ties break
 * on most-recently-reported.
 */
function chooseTab() {
  let best = null;
  for (const [tabId, entry] of live) {
    if (!entry.meta) continue;
    const video = freshVideo(entry);
    if (!video && !entry.hasPlayer) continue;
    const score = (video && !video.paused ? 1 : 0) * 1e15 + entry.metaAt;
    if (!best || score > best.score) best = { tabId, entry, video, score };
  }
  return best;
}

function reportFor(pick) {
  const m = pick.entry.meta;
  const v = pick.video;
  return {
    kind: m.kind || null,
    title: m.title,
    season: m.season ?? null,
    episode: m.episode ?? null,
    episode_title: m.episode_title || "",
    year: m.year ?? null,
    poster: m.poster || null,
    adapter: m.adapter || "",
    site: pick.entry.site || "",
    url: pick.entry.url || "",
    position: v ? v.position : null,
    duration: v ? v.duration : null,
    paused: v ? v.paused : false,
    quality: v ? v.quality : "",
  };
}

/** Everything that should trigger a fresh POST. Position is excluded: it
 *  changes constantly by design and the bridge derives timestamps from it. */
function signature(r) {
  return JSON.stringify([
    r.kind, r.title, r.season, r.episode, r.episode_title,
    r.year, r.poster, r.paused, r.quality,
  ]);
}

async function flush() {
  const pick = chooseTab();

  if (!pick) {
    if (publishing) {
      publishing = false;
      lastSig = null;
      await bridge("/v1/clear", {});
    }
    renderBadge();
    return;
  }

  const report = reportFor(pick);
  const sig = signature(report);
  const now = Date.now();
  if (sig === lastSig && now - lastPostAt < HEARTBEAT_MS) return;

  lastSig = sig;
  lastPostAt = now;
  publishing = true;
  await bridge("/v1/presence", report);
  renderBadge();
}

function drop(tabId) {
  if (live.delete(tabId)) flush();
}

// ---- messages -------------------------------------------------------------

async function handle(msg, sender) {
  const type = msg && msg.type;

  // --- from content scripts ---

  if (type === "nw:hello" || type === "nw:meta" || type === "nw:video" ||
      type === "nw:none") {
    const tabId = sender.tab && sender.tab.id;
    if (tabId === undefined) return { stop: true };

    // sender.tab.url is the TOP frame's url even when the message came from a
    // nested cross-origin iframe, which is exactly the gate we need: a player
    // frame is trusted only because of the page hosting it.
    const { sites } = await getState();
    if (!hostEnabled(hostOf(sender.tab.url), sites)) {
      // Tell it to stand down. In probe-frames mode this script is injected
      // into every frame on the web, and this is what keeps it inert.
      return { stop: true };
    }

    if (type === "nw:hello") {
      return { role: sender.frameId === 0 ? "meta" : "video" };
    }

    if (type === "nw:none") {
      drop(tabId);
      return { ok: true };
    }

    const entry = live.get(tabId) || {};
    if (type === "nw:meta") {
      entry.meta = msg.meta;
      entry.site = msg.site;
      entry.url = msg.url;
      entry.hasPlayer = !!msg.hasPlayer;
      entry.metaAt = Date.now();
      // A video in the top frame counts too; plenty of sites do not iframe.
      if (msg.video) {
        entry.video = msg.video;
        entry.videoAt = Date.now();
      }
    } else {
      entry.video = msg.video;
      entry.videoAt = Date.now();
    }
    live.set(tabId, entry);
    await flush();
    return { ok: true };
  }

  // --- from the popup ---

  if (type === "nw:state") {
    const state = await getState();
    let status = null;
    try {
      const res = await fetch(`http://127.0.0.1:${state.port}/v1/status`);
      status = res.ok ? await res.json() : null;
      bridgeOk = !!status;
    } catch {
      bridgeOk = false;
    }
    renderBadge();
    return { ...state, bridgeOk, publishing, status };
  }

  // chrome.permissions.request() must run inside a real user gesture, and a
  // service worker never has one, so the popup makes the call. The worker only
  // decides WHAT to ask for and records the result, which keeps the match
  // patterns defined in exactly one place.
  if (type === "nw:prepare") {
    const host = hostOf(msg.url);
    if (!host) return { ok: false, error: "not a normal web page" };
    return { ok: true, host, origins: patternsFor(host) };
  }

  if (type === "nw:enable-granted") {
    const host = hostOf(msg.url) || msg.host;
    if (!host) return { ok: false, error: "not a normal web page" };
    const { sites } = await getState();
    if (!sites.includes(host)) {
      await chrome.storage.local.set({ sites: [...sites, host] });
    }
    return { ...(await syncScripts()), host };
  }

  if (type === "nw:disable") {
    const host = msg.host;
    const { sites } = await getState();
    await chrome.storage.local.set({
      sites: sites.filter((s) => s !== host),
    });
    // Deliberately not calling permissions.remove(): a revoke tears down the
    // whole origin grant, and re-enabling would need a fresh user gesture. The
    // site list is the switch; the grant is just a capability we keep.
    return await syncScripts();
  }

  // Same gesture rule as above: the popup has already obtained (or declined to
  // obtain) the broad grant by the time this arrives.
  if (type === "nw:probe-granted") {
    await chrome.storage.local.set({ probeFrames: !!msg.on });
    return await syncScripts();
  }

  if (type === "nw:port") {
    await chrome.storage.local.set({ port: Number(msg.port) || 6788 });
    return { ok: true };
  }

  return { ok: false, error: "unknown message" };
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  // Skip the popup's own inspect calls, which are addressed to a tab.
  if (msg && msg.type === "nw:inspect") return;
  handle(msg, sender).then(reply, (e) => reply({ ok: false, error: String(e) }));
  return true; // async reply
});

chrome.tabs.onRemoved.addListener(drop);

chrome.tabs.onUpdated.addListener((tabId, info) => {
  // A committed navigation invalidates the merged state: the new page reports
  // fresh, and stale metadata from the old one must not linger.
  //
  // info.url is only populated when we hold permission for the tab, so status
  // is checked as well. status needs no permission at all, which makes it the
  // reliable half of this pair.
  if (info.url || info.status === "loading") drop(tabId);
});

chrome.runtime.onInstalled.addListener(async () => {
  const s = await chrome.storage.local.get(DEFAULTS);
  await chrome.storage.local.set({ ...DEFAULTS, ...s });
  await syncScripts();
  renderBadge();
});

chrome.runtime.onStartup.addListener(async () => {
  await syncScripts();
  renderBadge();
});
