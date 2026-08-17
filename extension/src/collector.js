/*
 * Runs in every frame it is injected into, and asks the service worker what its
 * job is before doing anything.
 *
 * The split exists because of one structural fact about streaming sites: the
 * title, season and episode live in the TOP frame, while the actual <video>
 * element lives inside a THIRD-PARTY player iframe on a different origin.
 * Nothing can read across that boundary, so neither frame can produce a
 * complete report alone.
 *
 *   top frame       -> metadata (title, season, episode, poster)
 *   player iframe   -> playback (position, duration, paused, height)
 *   background.js   -> merges the two by tabId
 *
 * Getting this wrong is why most homemade attempts show the title but never a
 * progress bar. The frames never talk to each other here; each reports
 * independently and the worker joins them.
 *
 * The role handshake matters for privacy as much as for correctness. In
 * "player frames" mode this script is registered against every url, so it is
 * injected into frames on sites that have nothing to do with watching
 * anything. It reads no DOM and sends no data until the worker confirms the
 * TOP frame of its tab is an enabled site, and the worker is the only party
 * that can know that, because a cross-origin child cannot see its parent's url.
 */

(() => {
  "use strict";

  // A second injection into the same frame would start a second interval and
  // double every report.
  if (globalThis.__nwCollectorLoaded) return;
  globalThis.__nwCollectorLoaded = true;

  // Two seconds is comfortably inside the bridge's staleness window while being
  // far below Discord's rate limit, which the bridge enforces anyway.
  const TICK_MS = 2000;

  // Ads, trailers and bumpers are short. A real film or episode is not.
  const MIN_DURATION = 60;

  const IS_TOP = window.top === window;

  let timer = null;
  let role = null;

  /**
   * The longest <video> in this frame.
   *
   * Duration is the discriminator rather than size or z-order: a pre-roll ad
   * can be styled to fill the player exactly like the feature, but it cannot be
   * forty minutes long.
   */
  function pickVideo() {
    let best = null;
    for (const v of document.querySelectorAll("video")) {
      const d = v.duration;
      if (!isFinite(d) || d <= MIN_DURATION) continue;
      if (!best || d > best.duration) best = v;
    }
    return best;
  }

  function videoSnapshot(v) {
    return {
      position: v.currentTime,
      duration: isFinite(v.duration) ? v.duration : null,
      paused: !!(v.paused || v.ended),
      // videoHeight is the height actually being decoded, which beats scraping
      // a quality badge: on an adaptive stream the badge often lies.
      quality: v.videoHeight ? `${v.videoHeight}p` : "",
    };
  }

  /**
   * Is there a player on this page at all?
   *
   * Without this, presence would fire on any detail page the user merely
   * browsed past. With no permission for the embed's origin we cannot see the
   * <video>, so the iframe itself stands in as evidence that playback started.
   */
  function hasPlayer() {
    if (document.querySelector("video")) return true;
    return !!document.querySelector(
      "#iframe-embed, .watching_player-area, iframe[allowfullscreen]"
    );
  }

  function teardown() {
    if (timer) clearInterval(timer);
    timer = null;
    role = "off";
  }

  /** Fire-and-forget, except that a `stop` reply shuts this frame down. */
  function send(msg) {
    if (role === "off") return;
    try {
      chrome.runtime.sendMessage(msg, (reply) => {
        // Touching lastError is what suppresses the "Unchecked runtime
        // lastError" console noise when the worker is asleep or was just
        // reloaded. Neither case is worth reporting.
        void chrome.runtime.lastError;
        if (reply && reply.stop) teardown();
      });
    } catch {
      teardown(); // extension context invalidated (reload or uninstall)
    }
  }

  function tick() {
    const video = pickVideo();

    if (role === "video") {
      // A subframe is only ever a playback source. With no qualifying video it
      // is an ad or a widget, and it stays silent.
      if (video) send({ type: "nw:video", video: videoSnapshot(video) });
      return;
    }

    if (role !== "meta") return;

    const meta = NW_ADAPTERS.read();
    if (!meta || meta.error) {
      send({ type: "nw:none", reason: (meta && meta.error) || "no adapter" });
      return;
    }

    send({
      type: "nw:meta",
      meta,
      hasPlayer: hasPlayer(),
      site: location.hostname.replace(/^www\./, ""),
      url: location.href,
      video: video ? videoSnapshot(video) : null,
    });
  }

  // The popup asks for this to show what the adapters actually see, which is
  // the fastest way to fix a selector on a mirror nobody has tried yet.
  // Registered regardless of role so a site can be inspected before enabling.
  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (!msg || msg.type !== "nw:inspect") return;
    const video = pickVideo();
    const adapters = typeof NW_ADAPTERS === "undefined" ? null : NW_ADAPTERS;
    const adapter = adapters && adapters.pick();
    reply({
      frame: IS_TOP ? "top" : "sub",
      role,
      adapter: adapter ? adapter.id : null,
      meta: IS_TOP && adapters ? adapters.read() : null,
      hasPlayer: IS_TOP ? hasPlayer() : false,
      video: video ? videoSnapshot(video) : null,
      videoCount: document.querySelectorAll("video").length,
      site: location.hostname.replace(/^www\./, ""),
    });
    return true;
  });

  // Nothing is read or sent until the worker assigns a role.
  try {
    chrome.runtime.sendMessage({ type: "nw:hello" }, (reply) => {
      void chrome.runtime.lastError;
      if (!reply || reply.stop || !reply.role) {
        role = "off";
        return;
      }
      // "meta" needs the adapter bundle. If only collector.js made it in, the
      // honest thing is to act as a playback source and let another frame (or
      // nothing) supply the title.
      role =
        reply.role === "meta" && typeof NW_ADAPTERS === "undefined"
          ? "video"
          : reply.role;
      timer = setInterval(tick, TICK_MS);
      tick();
    });
  } catch {
    role = "off";
  }
})();
