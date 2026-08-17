<h1 align="center">nowwatching</h1>

<p align="center">
  <b>Discord Rich Presence for streaming sites in your browser</b><br>
  <sub>The show's own name in the header, poster art, a live countdown,<br>and one adapter that covers every mirror of a template.</sub>
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-0078D4?style=flat-square">
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

## What makes the header the title

Most Rich Presence tools cannot help showing their own name. The bold top line
of an activity card is the Discord application's name, so a tool built around
one application ID says "Watching Netflix" or "Watching MyCoolTool" no matter
what is actually on screen.

It turns out `SET_ACTIVITY` accepts a `name` field that overrides that line.
So the header adapts per title:

```
Watching Breaking Bad          Watching Blade Runner 2049
S5:E14 - Ozymandias            2017 - Movie
1080p                          1080p
28:14 left                     1:52:07 left
```

Verified against the current Discord desktop client. If it ever regresses, set
`"header_mode": "app"` and the title moves down a line instead.

---

## Quick start

**1. Make a Discord application.**
[Developer Portal](https://discord.com/developers/applications) then **New
Application**. Name it anything, copy the **Application ID** from *General
Information*, and put it in `config.json`:

```json
{ "client_id": "1234567890123456789" }
```

No bot, no OAuth, no token. An Application ID is a public identifier, not a
secret. Because `name` overrides the header, the application's name is never
shown, so it genuinely does not matter what you call it.

**2. Start the bridge.**

```powershell
python nowwatching.py
```

Windows users can double-click `run.cmd` instead. Standard library only, so
there is nothing to `pip install`.

**3. Load the extension.**
Go to `chrome://extensions`, switch on **Developer mode**, click **Load
unpacked**, and pick the `extension/` folder.

**4. Enable a site.**
Open a streaming site, click the NowWatching toolbar icon, and press
**Enable**. Chrome asks for access to that one domain. Reload the page.

Presence appears a few seconds after playback starts.

---

## Requirements

| Need | Why |
|---|---|
| Discord **desktop** app | Rich Presence is a local IPC socket. It does not exist in a browser tab. |
| Python 3.7+ | The bridge. Standard library only. Developed and tested on 3.14. |
| A Chromium browser | Chrome, Edge, Brave, Opera. See [Firefox](#firefox). |

---

## What it shows

| Field | Where it comes from |
|---|---|
| Title in the header | The page's adapter, then `name` on the activity |
| Season and episode | The URL first, then the episode list, then the page title |
| Episode name | The active episode's `title` attribute |
| Year, for films | A parenthesised year in the page text |
| Poster art | The page's own `og:image`, passed to Discord as an external URL |
| Countdown and progress bar | The `<video>` element's `currentTime` and `duration` |
| Quality | `video.videoHeight`, which beats a quality badge that often lies |

The site you are watching on is **never published**. It is collected, because
the popup and the log need it to debug an adapter, but it stays on your machine
unless you set `show_site` to true.

---

## How it works

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
                      nowwatching.py
                              |
                      Discord IPC socket
```

Two design decisions carry most of the weight.

**Adapters match structure, not hostnames.** Streaming mirrors rotate domains
constantly, so a hardcoded domain list would need an update every time a new
one appears. `detect()` fingerprints the markup instead, so a fresh mirror of a
template the code already knows about works on day one. The sflix / fmovies /
movies2watch family are reskins of a single template, which is why one adapter
covers dozens of domains.

**The video and the metadata live in different frames.** On these sites the
title is in the top frame while the actual `<video>` sits inside a third-party
player iframe on another origin. Neither frame can read the other, so each
reports independently and the service worker joins them by tab. This is why
most homemade attempts show a title but never a progress bar.

The bridge only pushes to Discord when the state actually changes, spaced out
to stay under Discord's rate limit, so a tab playing quietly generates no
traffic at all.

---

## Permissions

The extension installs asking for **no site access**. Nothing is declared in
the manifest; content scripts are registered at runtime as you grant domains.
There are two levels.

**Enable (per site).** Grants one domain. Enough for the title, season,
episode and poster. You get the countdown too if that site's player is not
iframed.

**Read player frames (all sites).** Needed to reach a `<video>` inside a
third-party player iframe, because that frame is a different origin and cannot
be granted individually. The collector is written to be inert: in a frame it
asks the service worker for a role first, and the worker only assigns one if
the **top** frame of that tab is a site you enabled. Turn the switch off and
the grant is handed back.

The bridge listens on `127.0.0.1` only. It also checks the `Origin` header:
browsers always attach one to a cross-origin request and a page cannot forge
it, so an ordinary web page cannot drive your presence even though it can
reach the port.

---

## Configuration

Everything lives in `config.json`, next to `nowwatching.py`. It ships working
apart from `client_id`, so the rest is optional.

| Key | Default | Meaning |
|---|---|---|
| `client_id` | *(empty)* | Discord Application ID. Required. |
| `port` | `6788` | Where the bridge listens. Also settable in the popup. |
| `activity_type` | `3` | 3 = Watching, 2 = Listening, 0 = Playing |
| `header_mode` | `"title"` | `title` puts the show in the header; `app` falls back to the application name |
| `timestamp_mode` | `"remaining"` | `remaining` gives "28:14 left" and a progress bar; `elapsed` counts up; `off` |
| `show_poster` | `true` | Use the page's `og:image` as the large image |
| `show_site` | `false` | Publish the site you are watching on |
| `show_status_icon` | `false` | Small play/pause badge. Needs assets uploaded to your Discord app; see below. |
| `idle_clear_seconds` | `40` | Clear presence after this long without a report |
| `idle_exit_seconds` | `0` | Self-exit after this long idle. 0 never exits. |
| `debug` | `false` | Also log to stderr |

`timestamp_mode: "remaining"` needs `activity_type` 2 or 3. Discord rejects an
end timestamp on other types, and rejecting the timestamp loses the progress
bar. With `0` (Playing), use `"elapsed"`.

### The play/pause badge

Unlike the poster, the small badge cannot be an external URL. Upload two images
named `playing` and `paused` under **Rich Presence, Art Assets** on your Discord
application, then set `"show_status_icon": true`. Rename the keys with
`status_icon_keys` if you prefer.

---

## Adding a site

This is the expected contribution. Open the popup, press **Inspect page**, and
you get exactly what the adapters saw:

```json
{ "frame": "top", "adapter": "generic", "meta": {
    "title": "Some Show", "kind": "series", "season": null, "episode": 3 },
  "hasPlayer": true, "videoCount": 1 }
```

If `generic` already gets it right, there is nothing to add. It reads OpenGraph
tags plus the URL, which most streaming sites populate correctly because they
want link previews to work.

If it does not, copy the `sflix-family` block in
[`extension/src/adapters.js`](extension/src/adapters.js), change `detect()` to
fingerprint the markup you see, and return whatever `read()` can find. Every
field except `title` is optional, and the bridge range-checks all of it, so a
wrong guess degrades rather than breaks.

Reload the extension at `chrome://extensions` to pick up changes.

---

## Status

The Discord half is verified end to end against a live desktop client: the
`name` override, external-URL poster art, and the start/end timestamp pair that
draws the countdown were each confirmed with a real `SET_ACTIVITY` frame.
`python nowwatching.py --test` reproduces that check on your own machine.

The extension half is written but has not yet been run against every site
template it claims to handle. The `sflix-family` selectors in particular are
best effort: they are written as candidate lists because that template gets
forked and lightly renamed, and **Inspect page** exists so a broken selector
takes minutes to fix rather than an afternoon. Reports of what actually
happened on a given site are the most useful contribution right now.

---

## Troubleshooting

Check `run/nowwatching.log` first. A healthy session shows one `presence:` line
per real change, not a steady stream.

```
21:14:02 daemon: start on 127.0.0.1:6788
21:14:09 http: first request from Origin='chrome-extension://abcd...'
21:14:09 discord: connected via \\.\pipe\discord-ipc-0
21:14:09 presence: Breaking Bad | S5:E14 - Ozymandias | 1080p
```

<details>
<summary><b>Is it the browser half or the Discord half?</b></summary>

Split the problem:

```powershell
python nowwatching.py --test
```

Publishes one sample card with no browser involved. If that shows up, your
`client_id` and Discord are fine and the issue is in the extension. If it does
not, it never gets as far as the extension.

```powershell
python nowwatching.py --dry-run
```

Prints each payload instead of publishing, and runs happily alongside the real
bridge. Good for seeing whether the extension is reporting at all, and what.
</details>

<details>
<summary><b>Nothing shows up at all</b></summary>

Discord must be the **desktop app** and already running. Then check
`Settings, Activity Privacy, Share your detected activities` is on.

The popup's top-right pill tells you which half is missing: `bridge off` means
`nowwatching.py` is not running, `no discord` means the bridge is up but cannot
reach Discord.
</details>

<details>
<summary><b>The title shows but there is no countdown or progress bar</b></summary>

The player is in a cross-origin iframe, so the `<video>` is out of reach. Turn
on **Read player frames** in the popup and reload the page.

If it still does not appear, the player may be Flash-era or canvas-based with
no `<video>` element, in which case there is no clock to read.
</details>

<details>
<summary><b>Wrong title, or the site's name glued onto it</b></summary>

Press **Inspect page** to see the raw parse, then add a rule. Title cleaning
lives in `NOISE` and `cleanTitle` in `extension/src/adapters.js`, and season and
episode detection in `SE_BOTH` and `EP_ONLY` just below it.
</details>

<details>
<summary><b>Presence sticks around after I close the tab</b></summary>

It clears itself within `idle_clear_seconds` (40 by default). The extension
sends an explicit clear on navigation and tab close, but if the browser is
killed outright nobody gets to send anything, which is exactly what the
staleness timer is for. Lower it if 40 seconds feels long.
</details>

<details>
<summary><b>"port 6788 is busy"</b></summary>

Another copy is already running. The port is deliberately the single-instance
lock, so there is nothing else to clean up. Change `port` in `config.json` (and
in the popup) if something unrelated wants 6788.
</details>

<details>
<summary><b>Enabled a site but nothing happens</b></summary>

Content scripts are registered at the moment you grant permission, so a page
that was already open has no collector in it. Reload the page.
</details>

---

## Firefox

Untested. Firefox's MV3 support differs around `optional_host_permissions` and
runtime script registration, which is exactly the machinery this leans on.
The bridge is browser agnostic and already accepts `moz-extension://` origins,
so the work is confined to the extension. If you try it, please report back in
an issue.

---

## Acknowledgements

The Discord IPC layer is shared with
[anicli-rpc](https://github.com/KernelSpecter/anicli-rpc), which does the same
job for [ani-cli](https://github.com/pystardust/ani-cli) and mpv. If you watch
anime from a terminal, use that one instead. The comment on `bytes_available`
in both projects explains the one Windows detail that makes this work at all.

Poster art comes from the pages themselves. Nothing is uploaded anywhere, and
the bridge makes no outbound network calls.

MIT licensed. See [LICENSE](LICENSE).
