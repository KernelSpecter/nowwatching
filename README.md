<h1 align="center">nowwatching</h1>

<p align="center">
  <b>Discord Rich Presence for whatever you are watching</b><br>
  <sub>The show's own name in the header, poster art and a live countdown.<br>Run one file and play something. No extension required.</sub>
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square">
  <img alt="Python" src="https://img.shields.io/badge/python-3.7%2B-3776AB?style=flat-square">
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-none-5865F2?style=flat-square">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-green?style=flat-square"></a>
</p>

<p align="center">
  <img src="docs/preview.svg" alt="The nowwatching activity card on a Discord profile" width="470">
</p>

<!--
  ^ Placeholder mockup. To swap in a real screenshot:
      1. Start playing something, then screenshot your Discord profile card.
         Discord hides activity buttons on your OWN profile, so a second
         account or a mutual server's member list shows more.
      2. Save it as docs/preview.png
      3. Change the src above to docs/preview.png and drop the width if it
         looks small.
-->

---

## Quick start

**1. Put your Discord Application ID in `config.json`.**
[Developer Portal](https://discord.com/developers/applications) then **New
Application**. Name it anything at all, copy the **Application ID** from
*General Information*:

```json
{ "client_id": "1234567890123456789" }
```

No bot, no OAuth, no token. An Application ID is a public identifier, not a
secret.

**2. Run it.**

```powershell
python nowwatching.py
```

Windows users can double-click `run.cmd`. Standard library only, so there is
nothing to `pip install`.

**3. Play something.**

That is the whole setup. Presence appears within a few seconds.

The optional [browser extension](#the-browser-extension) improves what gets
shown on sites that report their titles badly, but nothing above needs it.

---

## Why the header says the show's name

Most Rich Presence tools cannot help announcing themselves. The bold top line
of an activity card is the Discord application's name, so a tool built around
one application ID reads "Watching Netflix" or "Watching SomeTool" no matter
what is actually on screen.

`SET_ACTIVITY` turns out to accept a `name` field that overrides that line, so
the header adapts per title:

```
Watching Breaking Bad          Watching Blade Runner 2049
S5:E14 - Ozymandias            2017 - Movie
1080p                          1080p
28:14 left                     1:52:07 left
```

Verified against the current Discord desktop client. If it ever regresses, set
`"header_mode": "app"` and the title moves down a line instead.

---

## Two sources

`source` in `config.json` picks between them. The default, `auto`, runs both and
prefers the extension whenever it is reporting.

| | `smtc` | `extension` |
|---|---|---|
| Setup | none | load unpacked, grant each site |
| Title | whatever the site reports to Windows | the page's own `og:title` and DOM |
| Season and episode | parsed out of the title string | read from the URL, which is exact |
| Poster | looked up by name | the page's own `og:image`, exact |
| Quality | not available | `video.videoHeight` |
| Covers | any browser, plus VLC, and anything else with a media session | only sites you enable |

**`smtc`** reads the Windows System Media Transport Controls session. Every
Chromium tab playing video registers one, which makes it the browser-side
equivalent of mpv's IPC socket: an OS-level interface, no per-site adapters, no
permissions.

Its one real weakness is that title quality is entirely up to the site. A site
implementing the Media Session API properly reports a real title. A site that
does not gets whatever Chromium infers, which is usually the page `<title>` and
can be as useless as the bare word `Instagram`. That gap is the entire reason
the extension still exists.

---

## What it shows

| Field | Where it comes from |
|---|---|
| Title in the header | the source's title, cleaned, then `name` on the activity |
| Season and episode | the URL if the extension is running, otherwise the title |
| Episode name | the active episode's `title` attribute (extension only) |
| Year, for films | a bracketed year in the title or page text |
| Poster art | the page's `og:image`, or a TMDB or AniList lookup |
| Countdown and progress bar | the player's position and duration |
| Quality | `video.videoHeight` (extension only) |

The site you are watching on is **never published**. It is collected, because
the popup and the log need it to debug an adapter, but it stays on your machine
unless you set `show_site` to true.

---

## Poster art

The extension scrapes the page's own `og:image`, which is exact and needs no
configuration. The SMTC path only has a title, and Discord's `large_image` takes
a URL rather than bytes, so it has to look one up.

| Provider | Key | Covers |
|---|---|---|
| **TMDB** | free key | film and TV, properly |
| **AniList** | none | anime only |

Put a TMDB key in `tmdb_api_key` and film and TV get posters. Without one,
AniList still covers anime out of the box, and everything else simply shows no
image.

IMDb has no free public API, which is why it is not in that table. TMDB carries
IMDb IDs for every title, so nothing is lost by going through it.

The keyless **iTunes Search** API was measured before being ruled out: 3 of 8
test titles matched, one of those was an outright wrong match (`The Bear`
resolved to an unrelated documentary), and every film missed. It is
region-dependent and not usable here.

Lookups run on a background thread and are cached in `run/`, so nothing about
this ever delays a presence update.

---

## Configuration

Everything lives in `config.json`, next to `nowwatching.py`. It ships working
apart from `client_id`.

| Key | Default | Meaning |
|---|---|---|
| `client_id` | *(empty)* | Discord Application ID. Required. |
| `source` | `"auto"` | `auto`, `smtc` or `extension`. See [Two sources](#two-sources). |
| `port` | `6788` | Where the bridge listens for the extension. |
| `activity_type` | `3` | 3 = Watching, 2 = Listening, 0 = Playing |
| `header_mode` | `"title"` | `title` puts the show in the header; `app` falls back to the application name |
| `timestamp_mode` | `"remaining"` | `remaining` gives "28:14 left" and a progress bar; `elapsed` counts up; `off` |
| `show_poster` | `true` | Use poster art as the large image |
| `show_site` | `false` | Publish the site you are watching on |
| `show_status_icon` | `false` | Small play/pause badge. Needs uploaded assets; see below. |
| `poster_lookup` | `true` | Look up posters the source could not supply |
| `tmdb_api_key` | *(empty)* | Enables film and TV posters |
| `smtc_min_duration` | `60` | Ignore anything shorter, so a reel or an ad is not announced |
| `smtc_ignore_apps` | Spotify, Groove, ... | Apps whose media is listened to, not watched |
| `idle_clear_seconds` | `40` | Clear presence after this long without a report |
| `idle_exit_seconds` | `0` | Self-exit after this long idle. 0 never exits. |
| `debug` | `false` | Also log to stderr |

`timestamp_mode: "remaining"` needs `activity_type` 2 or 3. Discord rejects an
end timestamp on other types, and losing the timestamp loses the progress bar.
With `0` (Playing), use `"elapsed"`.

### The play/pause badge

Unlike the poster, the small badge cannot be an external URL. Upload two images
named `playing` and `paused` under **Rich Presence, Art Assets** on your Discord
application, then set `"show_status_icon": true`.

---

## The browser extension

Optional. It exists to fix the sites SMTC reports badly, and to add the exact
poster, the URL-derived season and episode, and the quality readout.

Go to `chrome://extensions`, switch on **Developer mode**, click **Load
unpacked**, and pick the `extension/` folder. Then open a streaming site, click
the toolbar icon, and press **Enable**.

### How it works

```
      top frame                   player iframe (cross-origin)
   title, season, ep                position, duration, paused
          |                                    |
          +------------- content script -------+
                              |
                     background.js  (merges by tabId)
                              |
                     POST 127.0.0.1:6788
                              |
                      nowwatching.py  <---- smtc.ps1 (the other source)
                              |
                      Discord IPC socket
```

Two decisions carry most of the weight.

**Adapters match structure, not hostnames.** Streaming mirrors rotate domains
constantly, so a hardcoded domain list would need an update every time a new one
appears. `detect()` fingerprints the markup instead, so a fresh mirror of a
template the code already knows works on day one. The sflix, fmovies and
movies2watch family are reskins of a single template, which is why one adapter
covers dozens of domains.

**The video and the metadata live in different frames.** The title is in the top
frame while the actual `<video>` sits inside a third-party player iframe on
another origin. Neither frame can read the other, so each reports independently
and the service worker joins them by tab. This is why most homemade attempts
show a title but never a progress bar.

### Permissions

The extension installs asking for **no site access**. Nothing is declared in the
manifest; content scripts are registered at runtime as you grant domains.

**Enable (per site)** grants one domain, enough for title, season, episode and
poster.

**Read player frames (all sites)** is needed to reach a `<video>` inside a
third-party player iframe, because that frame is a different origin and cannot be
granted individually. The collector asks the service worker for a role before
reading anything, and the worker only assigns one if the **top** frame of that
tab is a site you enabled. Switch it off and the grant is handed back.

The bridge listens on `127.0.0.1` only and checks the `Origin` header on reads as
well as writes: browsers always attach one to a cross-origin request and a page
cannot forge it, so an ordinary web page can neither drive your presence nor read
what you are watching.

### Adding a site

Open the popup and press **Inspect page** to see exactly what the adapters saw:

```json
{ "frame": "top", "adapter": "generic", "meta": {
    "title": "Some Show", "kind": "series", "season": null, "episode": 3 },
  "hasPlayer": true, "videoCount": 1 }
```

If `generic` already gets it right there is nothing to add. It reads OpenGraph
tags plus the URL, which most streaming sites populate correctly because they
want link previews to work.

If not, copy the `sflix-family` block in
[`extension/src/adapters.js`](extension/src/adapters.js), change `detect()` to
fingerprint the markup you see, and return whatever `read()` can find. Every
field except `title` is optional and the bridge range-checks all of it, so a
wrong guess degrades rather than breaks. Reload the extension to pick up changes.

---

## Status

**Verified end to end** against a live Discord desktop client: the `name` header
override, external-URL poster art, the start and end timestamp pair that draws
the countdown, all four payload layouts, and the Origin checks. The SMTC helper
was confirmed reporting real title, position, duration and playback status from a
live browser tab, and its Python side has a deterministic test covering
extrapolation, the duration floor, the app filter and staleness.

**Not yet verified:** the browser extension has never been loaded in a browser.
It is syntax-checked but no live page has been parsed by it, and the
`sflix-family` selectors in particular are best effort, written as candidate
lists because that template gets forked and lightly renamed. **Inspect page**
exists so a broken selector takes minutes to fix. Reports of what actually
happened on a given site are the most useful contribution right now.

---

## Troubleshooting

Check `run/nowwatching.log` first. A healthy session shows one `presence:` line
per real change, not a steady stream.

```
21:14:02 daemon: start on 127.0.0.1:6788
21:14:02 smtc: helper started
21:14:09 discord: connected via \\.\pipe\discord-ipc-0
21:14:09 presence: Breaking Bad | S5:E14 - Ozymandias | 1080p  [smtc:MSEdge]
```

The trailing bracket names the source, so you can always tell which half
produced a card.

<details>
<summary><b>Is it the browser half or the Discord half?</b></summary>

```powershell
python nowwatching.py --test
```

Publishes one sample card with no browser involved. If that shows up, your
`client_id` and Discord are fine and the problem is upstream of them.

```powershell
python nowwatching.py --dry-run
```

Prints each payload instead of publishing, and runs alongside the real bridge.

```powershell
python smtc.py
```

Prints the media session as the daemon sees it, once a second. If this says
`(nothing)` while a video plays, the site is not registering a session and only
the extension can help.
</details>

<details>
<summary><b>Nothing shows up at all</b></summary>

Discord must be the **desktop app** and already running. Then check
`Settings, Activity Privacy, Share your detected activities` is on.
</details>

<details>
<summary><b>It says "Watching Instagram", or some other site name</b></summary>

That site reports no Media Session metadata, so Windows only knows the page
title. Nothing on the SMTC side can recover a real title from that. Load the
extension and enable that site.
</details>

<details>
<summary><b>Short clips and ads keep appearing</b></summary>

Raise `smtc_min_duration`. It defaults to 60 seconds, which filters reels and
most pre-rolls but not a long ad break.
</details>

<details>
<summary><b>No poster on films</b></summary>

Expected without a TMDB key: AniList is the only keyless provider and it covers
anime only. Add `tmdb_api_key`, or load the extension, which takes the poster
straight off the page.
</details>

<details>
<summary><b>Music shows up as something I am "watching"</b></summary>

Add the app to `smtc_ignore_apps`. Spotify and Groove are filtered by default.
</details>

<details>
<summary><b>The title shows but there is no countdown</b></summary>

On the extension path, the player is in a cross-origin iframe so the `<video>`
is out of reach. Turn on **Read player frames** and reload the page.
</details>

<details>
<summary><b>"port 6788 is busy"</b></summary>

Another copy is already running. The port is deliberately the single-instance
lock. Change `port` in `config.json` (and in the popup) if something unrelated
wants it.
</details>

---

## Linux and macOS

The SMTC source is Windows only: it is a WinRT API with no equivalent elsewhere.
The bridge and the extension are both portable, so `"source": "extension"` works
anywhere, and the bridge already accepts `moz-extension://` origins. MPRIS would
be the Linux analogue of `smtc.ps1` and is not written. If you want it, that is
the most useful thing to contribute.

---

## Acknowledgements

The Discord IPC layer is shared with
[anicli-rpc](https://github.com/KernelSpecter/anicli-rpc), which does this job
for [ani-cli](https://github.com/pystardust/ani-cli) and mpv. If you watch anime
from a terminal, use that one instead. The comment on `bytes_available` in both
projects explains the one Windows detail that makes any of this work.

Metadata from [TMDB](https://www.themoviedb.org) and
[AniList](https://anilist.co). This product uses the TMDB API but is not
endorsed or certified by TMDB.

MIT licensed. See [LICENSE](LICENSE).
