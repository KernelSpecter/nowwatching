r"""Windows media session source: presence with no browser extension at all.

Every Chromium tab playing video registers a System Media Transport Controls
session, and so do VLC, Spotify and anything else implementing SMTC. That makes
it the browser-side equivalent of mpv's IPC socket: an OS-level, machine-readable
surface, with no per-site adapters and no permissions to grant.

Two things about it are worth knowing before relying on it.

* Title quality is entirely up to the site. A site that implements the Media
  Session API properly reports a real title ("Frieren: Beyond Journey's End,
  Episode 4"). A site that does not gets whatever Chromium infers, which is
  usually the page <title> and can be as useless as the bare word "Instagram".
  This is exactly the gap the browser extension fills, which is why both
  sources exist rather than one replacing the other.

* Position is a snapshot, not a clock. SMTC refreshes it only when the app
  pushes an update, so a naive read drifts behind real playback. `positionAt`
  carries the moment it was measured and snapshot() extrapolates from it.

SMTC is WinRT, which Python cannot reach without a dependency, so the reading is
done by smtc.ps1 and streamed here as newline-delimited JSON. One long-lived
process, not one per poll, which keeps this project standard-library only.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER = HERE / "smtc.ps1"

# Apps whose media is listened to rather than watched. Without this, a Spotify
# track publishes as "Watching <song>", which is worse than publishing nothing.
DEFAULT_IGNORE_APPS = (
    "spotify",
    "zune",            # Groove / Windows Media Player's app id
    "aimp",
    "foobar2000",
    "musicbee",
)


# --------------------------------------------------------------------------
# title parsing
#
# Ported from the extension's adapters.js so both sources produce the same
# shape. The extension has the URL and the DOM to work from; here there is only
# a title string, so these patterns are doing more of the work alone.
# --------------------------------------------------------------------------

NOISE = [
    re.compile(r"\bwatch\s+(full\s+)?(movie|series|episode|show)s?\s+online\b", re.I),
    re.compile(r"\b(full\s+)?(movie|episode)\s+online\s+free\b", re.I),
    re.compile(r"\bwatch\s+online\s+free\b", re.I),
    re.compile(r"\bfull\s+(movie|episode|series)\b", re.I),
    re.compile(r"\bstream(ing)?\s+online\b", re.I),
    re.compile(r"\bwatch\s+online\b", re.I),
    re.compile(r"\b(online\s+)?for\s+free\b", re.I),
    re.compile(r"\bonline\s+free\b", re.I),
    re.compile(r"\bfree\s+online\b", re.I),
    re.compile(r"\bin\s+hd\b", re.I),
    re.compile(r"\b(full\s+)?(hd|uhd|4k|2160p|1080p|720p|480p)\b", re.I),
    # A literal resolution. Stripped here rather than left to the season/episode
    # patterns, whose digit caps stop "1920x1080" being read as S1920E1080 but
    # would otherwise leave it sitting in the title.
    re.compile(r"\b\d{3,4}\s*x\s*\d{3,4}\b", re.I),
    re.compile(r"\bno\s+ads?\b", re.I),
    # YouTube and friends bolt these on.
    re.compile(r"\s*[-|]\s*youtube\s*$", re.I),
    re.compile(r"\bofficial\s+(trailer|video|music\s+video)\b", re.I),
]

SEP = re.compile(r"\s+[|•·–—>]+\s+|\s+-\s+")

SE_BOTH = [
    re.compile(r"\bseason[\s-]*(\d{1,2})[\s-]*episode[\s-]*(\d{1,4})\b", re.I),
    re.compile(r"\bs\s*(\d{1,2})\s*[:.\s]*\s*e\s*p?\s*(\d{1,4})\b", re.I),
    # Both "s3x10" and the bare "3x10" that fan listings use. The episode half
    # is capped at three digits so a resolution ("1920x1080") cannot match; the
    # leading \b already rules that one out, and the cap keeps it ruled out if
    # the season half is ever widened.
    re.compile(r"\bs?(\d{1,2})x(\d{1,3})\b", re.I),
]

EP_ONLY = [
    re.compile(r"\bepisode[\s-]*(\d{1,4})\b", re.I),
    re.compile(r"\beps?\.?\s*(\d{1,4})\b", re.I),
]

SEASON_ONLY = [re.compile(r"\bseason[\s-]*(\d{1,2})\b", re.I)]

# Only a bracketed year counts. A bare four-digit match reads "Blade Runner
# 2049" and "1917" as release years.
YEAR = re.compile(r"[(\[]\s*((?:19|20)\d{2})\s*[)\]]")


def clean_title(raw):
    t = str(raw or "")
    t = re.sub(r"^\s*(watch|stream|play)\s+", "", t, flags=re.I)

    # Keep the leading segment before a separator, but only when what is left is
    # still substantial: "Mr. Robot - Season 1" should lose the tail, while
    # "9-1-1" must not become "9".
    head = SEP.split(t)[0]
    if head and len(head.strip()) >= 3:
        t = head

    for pat in NOISE:
        t = pat.sub(" ", t)

    t = re.sub(r"\(\s*\)", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" \t-|:·–—")


def find_season_episode(haystacks):
    for h in haystacks:
        if not h:
            continue
        for pat in SE_BOTH:
            m = pat.search(h)
            if m:
                return int(m.group(1)), int(m.group(2))
    season = episode = None
    for h in haystacks:
        if not h:
            continue
        if episode is None:
            for pat in EP_ONLY:
                m = pat.search(h)
                if m:
                    episode = int(m.group(1))
                    break
        if season is None:
            for pat in SEASON_ONLY:
                m = pat.search(h)
                if m:
                    season = int(m.group(1))
                    break
    return season, episode


def find_year(haystacks):
    for h in haystacks:
        if not h:
            continue
        m = YEAR.search(h)
        if m:
            return int(m.group(1))
    return None


def parse_title(title, artist=""):
    """Best effort structured report from a title string and nothing else."""
    hay = [title or "", artist or ""]
    season, episode = find_season_episode(hay)
    cleaned = clean_title(title)
    # Strip the markers now that they have been captured, so the title line does
    # not repeat what the details line already says. The year goes too: it is
    # published as part of the movie details, and "Blade Runner 2049 (2017)"
    # reads as though the year were part of the name.
    for pat in SE_BOTH + EP_ONLY + SEASON_ONLY + [YEAR]:
        cleaned = pat.sub(" ", cleaned)
    cleaned = re.sub(r"\(\s*\)", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" \t-|:,·–—")
    return {
        "title": cleaned or clean_title(title),
        "kind": "series" if (season or episode) else "movie",
        "season": season,
        "episode": episode,
        "episode_title": "",
        "year": find_year(hay),
    }


# --------------------------------------------------------------------------
# helper process
# --------------------------------------------------------------------------

class SmtcSource:
    """Reads smtc.ps1's NDJSON stream and exposes the latest snapshot.

    Not available off Windows, and `available` stays False there rather than
    raising, so the daemon runs unchanged on Linux with the extension as its
    only source.
    """

    def __init__(self, log, min_duration=60.0, ignore_apps=None,
                 interval=1.0):
        self.log = log
        self.min_duration = float(min_duration)
        self.ignore_apps = tuple(
            a.lower() for a in (ignore_apps
                                if ignore_apps is not None
                                else DEFAULT_IGNORE_APPS)
        )
        self.interval = interval
        self.proc = None
        self.available = False
        self._q = queue.Queue()
        self._latest = None
        self._latest_at = 0.0
        self._lock = threading.Lock()
        self._warned = set()

    def start(self):
        if os.name != "nt":
            self.log("smtc: not Windows, source disabled")
            return False
        if not HELPER.is_file():
            self.log(f"smtc: helper missing at {HELPER}")
            return False
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(HELPER), "-IntervalSeconds", str(self.interval)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # No console window when the daemon is launched from a shortcut.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            self.log(f"smtc: could not start helper ({e})")
            return False

        threading.Thread(target=self._read, daemon=True).start()
        self.available = True
        self.log("smtc: helper started")
        return True

    def _read(self):
        """Drain the helper's stdout. readline blocks, hence its own thread."""
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("error"):
                    self._warn_once("error", f"smtc: {msg['error']}")
                    continue
                if msg.get("ready"):
                    continue
                with self._lock:
                    self._latest = msg
                    self._latest_at = time.time()
        except (OSError, ValueError):
            pass
        finally:
            self.available = False
            self.log("smtc: helper stream ended")

    def _warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            self.log(msg)

    def stop(self):
        self.available = False
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None

    def revision(self):
        """When the newest helper line arrived.

        The daemon uses this to decide whether to rebuild the activity. Position
        is extrapolated inside snapshot(), so between lines the derived start
        timestamp is stable and there is nothing to republish.
        """
        with self._lock:
            return self._latest_at

    def snapshot(self):
        """Latest session as a report dict, or None when nothing qualifies."""
        with self._lock:
            msg = self._latest
            at = self._latest_at

        if not msg or not msg.get("session"):
            return None
        # The helper polls every second; anything older means it has wedged.
        if time.time() - at > 10.0:
            return None

        app = (msg.get("app") or "").lower()
        if any(bad in app for bad in self.ignore_apps):
            return None

        title = (msg.get("title") or "").strip()
        if len(title) < 2:
            return None

        duration = msg.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            # A seven second clip is a reel or an ad, not something worth
            # announcing. Unknown durations (live streams) are let through.
            if duration < self.min_duration:
                return None
        else:
            duration = None

        status = (msg.get("status") or "").lower()
        paused = status != "playing"

        position = msg.get("position")
        if not isinstance(position, (int, float)):
            position = None
        elif not paused:
            # Extrapolate: SMTC only refreshes position when the app pushes an
            # update, so without this the countdown sits behind real playback by
            # however long ago that was.
            measured_at = msg.get("positionAt")
            if isinstance(measured_at, (int, float)):
                drift = time.time() - measured_at
                if 0 <= drift < 3600:
                    position = position + drift
            if duration:
                position = min(position, duration)

        report = parse_title(title, msg.get("artist") or "")
        report.update({
            "site": "",           # SMTC has no url, and the site is not
            "url": "",            # published anyway
            "poster": None,       # a WinRT stream, not a url; see metadata.py
            "position": position,
            "duration": duration,
            "paused": paused,
            "adapter": f"smtc:{msg.get('app') or '?'}",
            "quality": "",        # not exposed by SMTC
        })
        return report


if __name__ == "__main__":
    # Manual check: python smtc.py
    src = SmtcSource(lambda m: print(m, flush=True))
    if not src.start():
        sys.exit(1)
    try:
        while True:
            time.sleep(1.5)
            snap = src.snapshot()
            print(json.dumps(snap, ensure_ascii=False) if snap else "(nothing)",
                  flush=True)
    except KeyboardInterrupt:
        src.stop()
