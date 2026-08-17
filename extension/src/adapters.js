/*
 * Adapter registry: page DOM in, a normalised report out.
 *
 * Two things about this file are worth understanding before you edit it.
 *
 * 1. Adapters match on page STRUCTURE, not on hostname. Streaming mirrors
 *    rotate domains constantly, so a hardcoded domain list is dead on arrival
 *    and would need a store review every time mirror #47 appears. detect()
 *    fingerprints the markup instead, so a brand new mirror of a template this
 *    file already knows about works on day one with no code change.
 *
 * 2. The `generic` adapter is the workhorse, not the fallback of last resort.
 *    It reads OpenGraph tags plus the URL, which most of these sites populate
 *    correctly because they want link previews to work. A site-specific
 *    adapter only has to beat that baseline, so keep them small.
 *
 * Adding a site is the expected contribution here. Copy the sflix-family
 * block, change detect(), and return whatever read() can find. Every field
 * except `title` is optional, and the bridge range-checks all of it anyway.
 */

// `var` plus the short-circuit, rather than `const`, so a second injection into
// the same frame is a no-op instead of a "NW_ADAPTERS has already been declared"
// SyntaxError that would take the collector down with it.
var NW_ADAPTERS = globalThis.NW_ADAPTERS || (() => {
  "use strict";

  // ---- small DOM helpers -------------------------------------------------

  /** First selector that matches and has non-empty text. */
  function pickText(selectors, root) {
    root = root || document;
    for (const sel of selectors) {
      let el;
      try {
        el = root.querySelector(sel);
      } catch {
        continue; // a bad selector must not take the whole adapter down
      }
      const t = el && (el.textContent || "").trim();
      if (t) return t;
    }
    return "";
  }

  /** First selector that matches and has a non-empty attribute. */
  function pickAttr(selectors, name, root) {
    root = root || document;
    for (const sel of selectors) {
      let el;
      try {
        el = root.querySelector(sel);
      } catch {
        continue;
      }
      const v = el && (el.getAttribute(name) || "").trim();
      if (v) return v;
    }
    return "";
  }

  /** OpenGraph or twitter card value. Checks both property= and name=. */
  function metaTag(prop) {
    return pickAttr(
      [`meta[property="${prop}"]`, `meta[name="${prop}"]`],
      "content"
    );
  }

  // ---- title cleaning ----------------------------------------------------

  // Site chrome that shows up glued onto a title. Ordered: the longest, most
  // specific phrases first, so "Watch Full Movie Online Free" does not get
  // half-eaten by the "Free" rule and leave debris behind.
  const NOISE = [
    /\bwatch\s+(full\s+)?(movie|series|episode|show)s?\s+online\b/gi,
    /\b(full\s+)?(movie|episode)\s+online\s+free\b/gi,
    /\bwatch\s+online\s+free\b/gi,
    /\bfull\s+(movie|episode|series)\b/gi,
    /\bstream(ing)?\s+online\b/gi,
    /\bwatch\s+online\b/gi,
    /\b(online\s+)?for\s+free\b/gi,
    /\bonline\s+free\b/gi,
    /\bfree\s+online\b/gi,
    /\bin\s+hd\b/gi,
    /\b(full\s+)?(hd|uhd|4k|2160p|1080p|720p|480p)\b/gi,
    /\bsub(bed)?\s*\|\s*dub(bed)?\b/gi,
    /\bno\s+ads?\b/gi,
  ];

  // Separators sites use before their own name: "Breaking Bad | SiteName".
  const SEP = /\s+[|•·–—>]+\s+|\s+-\s+/;

  function cleanTitle(raw) {
    let t = String(raw || "");

    t = t.replace(/^\s*(watch|stream|play)\s+/i, "");

    // Take the leading segment before a separator, but only if what remains is
    // still substantial. "Mr. Robot - Season 1" would otherwise become
    // "Mr. Robot" (fine) while "9-1-1" must not become "9".
    const head = t.split(SEP)[0];
    if (head && head.trim().length >= 3) t = head;

    for (const re of NOISE) t = t.replace(re, " ");

    return t
      .replace(/\(\s*\)/g, " ")
      .replace(/\s{2,}/g, " ")
      .replace(/^[\s\-|:·–—]+|[\s\-|:·–—]+$/g, "")
      .trim();
  }

  // ---- season / episode --------------------------------------------------

  // Both groups captured, season first.
  const SE_BOTH = [
    /\/season[/-](\d{1,2})\/episode[/-](\d{1,4})/i,
    /\bseason[\s-]*(\d{1,2})[\s-]*episode[\s-]*(\d{1,4})\b/i,
    /\bs\s*(\d{1,2})\s*[:.\s]*\s*e\s*p?\s*(\d{1,4})\b/i,
    /\bs(\d{1,2})x(\d{1,4})\b/i,
  ];

  // Episode only, for sites that keep the season in a separate control.
  const EP_ONLY = [
    /\bepisode[\s-]*(\d{1,4})\b/i,
    /\beps?\.?\s*(\d{1,4})\b/i,
  ];

  const SEASON_ONLY = [/\bseason[\s-]*(\d{1,2})\b/i, /\bs(\d{1,2})\b/i];

  /**
   * Scan several haystacks in confidence order and return {season, episode}.
   * The URL beats page text: it is set by the site's own router, while a title
   * can carry a number that belongs to the show's name.
   */
  function findSeasonEpisode(haystacks) {
    for (const h of haystacks) {
      if (!h) continue;
      for (const re of SE_BOTH) {
        const m = h.match(re);
        if (m) return { season: +m[1], episode: +m[2] };
      }
    }
    let season = null;
    let episode = null;
    for (const h of haystacks) {
      if (!h) continue;
      if (episode === null) {
        for (const re of EP_ONLY) {
          const m = h.match(re);
          if (m) {
            episode = +m[1];
            break;
          }
        }
      }
      if (season === null) {
        for (const re of SEASON_ONLY) {
          const m = h.match(re);
          if (m) {
            season = +m[1];
            break;
          }
        }
      }
    }
    return { season, episode };
  }

  /**
   * Only a parenthesised or bracketed year counts. A bare four-digit match
   * would read "Blade Runner 2049" and "1917" as release years.
   */
  function findYear(haystacks) {
    for (const h of haystacks) {
      if (!h) continue;
      const m = String(h).match(/[([](\s*(?:19|20)\d{2}\s*)[)\]]/);
      if (m) return +m[1].trim();
    }
    return null;
  }

  // ---- poster ------------------------------------------------------------

  /**
   * og:image first: it is one tag, it is what the site itself considers the
   * canonical artwork, and it is already an absolute url.
   *
   * Failing that, hunt for a poster by SHAPE. Cover art is close to 2:3, so an
   * aspect-ratio window plus a size floor finds it without knowing a single
   * thing about the site's class names.
   */
  function findPoster() {
    const og = metaTag("og:image") || metaTag("twitter:image");
    if (og && /^https?:\/\//i.test(og)) return og;

    let best = null;
    let bestArea = 0;
    for (const img of document.images) {
      const w = img.naturalWidth;
      const h = img.naturalHeight;
      if (!w || !h || w < 120 || h < 180) continue;
      const ratio = h / w;
      if (ratio < 1.3 || ratio > 1.7) continue;
      const area = w * h;
      if (area > bestArea) {
        bestArea = area;
        best = img.currentSrc || img.src;
      }
    }
    return best && /^https?:\/\//i.test(best) ? best : null;
  }

  // ---- kind --------------------------------------------------------------

  function findKind(haystacks) {
    const ogType = (metaTag("og:type") || "").toLowerCase();
    if (ogType.includes("tv") || ogType.includes("episode")) return "series";
    if (ogType.includes("movie")) return "movie";

    const path = location.pathname.toLowerCase();
    if (/\b(tv|series|show|anime|episode)\b/.test(path)) return "series";
    if (/\bmovies?\b/.test(path)) return "movie";

    const se = findSeasonEpisode(haystacks);
    return se.season || se.episode ? "series" : "movie";
  }

  // ---- adapters ----------------------------------------------------------

  const ADAPTERS = [
    {
      /*
       * The sflix / fmovies / movies2watch family. These are reskins of one
       * template, which is why a single adapter covers dozens of domains.
       *
       * Selectors are written as candidate lists on purpose: the template gets
       * forked and lightly renamed, so any single selector is a coin flip
       * while four of them together are reliable. If a mirror breaks, open the
       * popup, hit "Inspect page", and add the selector you actually see.
       */
      id: "sflix-family",

      detect() {
        return !!(
          document.querySelector(".detail_page-watch, #detail-container") ||
          (document.querySelector(".heading-name") &&
            document.querySelector("#iframe-embed, .watching_player-area"))
        );
      },

      read() {
        const title = pickText([
          ".heading-name a",
          ".heading-name",
          "h2.heading-name",
          ".detail_page-infor h2",
        ]);

        // "Eps 14: Ozymandias" lives in the active episode's title attribute.
        const epRaw =
          pickAttr(
            [
              ".ss-list a.ep-item.active",
              ".ss-list a.ssl-item.active",
              ".episodes-list .active",
            ],
            "title"
          ) ||
          pickText([".ss-list a.ep-item.active", ".ss-list a.ssl-item.active"]);

        const seasonRaw = pickText([
          ".dropdown-menu .dropdown-item.active",
          ".ss-choice .active",
          ".seasons-dropdown .active",
        ]);

        const haystacks = [location.href, epRaw, seasonRaw, document.title];
        const se = findSeasonEpisode(haystacks);

        // Strip the "Eps 14:" prefix to leave just the episode's own name.
        const epTitle = epRaw
          .replace(/^\s*eps?\.?\s*\d+\s*[:.\-]?\s*/i, "")
          .trim();

        return {
          title: cleanTitle(title || metaTag("og:title") || document.title),
          kind: se.season || se.episode || epRaw ? "series" : findKind(haystacks),
          season: se.season,
          episode: se.episode,
          episode_title: epTitle,
          year: findYear([
            pickText([".detail_page-infor", ".elements", ".film-infor"]),
            document.title,
          ]),
          poster: findPoster(),
        };
      },
    },

    {
      /*
       * Structure-free baseline. Reads OpenGraph plus the URL, which is enough
       * for most streaming sites and for plenty of legitimate ones too.
       */
      id: "generic",

      detect() {
        return true;
      },

      read() {
        const ogTitle = metaTag("og:title");
        const haystacks = [location.href, document.title, ogTitle];
        const se = findSeasonEpisode(haystacks);

        return {
          title: cleanTitle(ogTitle || document.title),
          kind: findKind(haystacks),
          season: se.season,
          episode: se.episode,
          episode_title: "",
          year: findYear([ogTitle, document.title]),
          poster: findPoster(),
        };
      },
    },
  ];

  /**
   * First adapter whose detect() passes. A throwing detect() is skipped rather
   * than fatal, so one broken adapter cannot blind the whole extension.
   */
  function pick() {
    for (const a of ADAPTERS) {
      try {
        if (a.detect()) return a;
      } catch {
        /* next */
      }
    }
    return null;
  }

  /** Run the winning adapter. Returns null when nothing usable was found. */
  function read() {
    const adapter = pick();
    if (!adapter) return null;
    let out;
    try {
      out = adapter.read() || {};
    } catch (e) {
      return { adapter: adapter.id, error: String(e) };
    }
    if (!out.title) return { adapter: adapter.id, error: "no title found" };
    out.adapter = adapter.id;
    return out;
  }

  return { read, pick, cleanTitle, findSeasonEpisode, findPoster, ADAPTERS };
})();

globalThis.NW_ADAPTERS = NW_ADAPTERS;
