r"""nowwatching - Discord Rich Presence for streaming sites in your browser.

A browser extension reads what is on screen and POSTs it to this daemon, which
turns it into a Discord activity:

    [poster]  Watching Breaking Bad
              S5:E14 - Ozymandias
              1080p
              [======-----]  28:14 left

The site you are watching on is deliberately never published. It is collected
(the popup and the log need it to debug an adapter) but `show_site` defaults to
false, so it stays on your machine.

Design notes worth knowing before editing:

* The Discord IPC layer (bytes_available, PipeReader, DiscordRPC) is shared
  with anicli-rpc; keep the two in sync. The non-obvious part is documented on
  bytes_available: a synchronously opened Windows named-pipe handle serialises
  its operations, so a thread parked in a blocking read makes a concurrent
  write queue behind it forever. Every read is therefore preceded by
  PeekNamedPipe and never asks for more bytes than are already buffered.
* `name` on the activity overrides the top line, so the header reads
  "Watching Breaking Bad" instead of the Discord application's own name. That
  is verified against the current desktop client. `header_mode: "app"` is the
  fallback layout if it ever regresses.
* Activity type 3 (Watching) is required for an `end` timestamp to be
  accepted, and start+end together are what draw the progress bar and the
  live countdown. Type 0 (Playing) silently loses both.
* Discord rate-limits SET_ACTIVITY to roughly 5 per 20s. Presence is pushed
  only on a real state change, spaced by MIN_PUSH_INTERVAL, so an idle
  playing tab produces no traffic at all.
* HTTP runs on its own threads. The Discord pipe is touched only by the main
  loop, and reports cross that boundary through Inbox, which is lock-guarded.
* Every field in a report came out of a web page's DOM, so normalise()
  treats all of it as hostile: strings clipped, numbers range-checked, urls
  scheme-checked, unknown keys dropped.
"""

import json
import os
import queue
import struct
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.1.0"


def base_dir():
    """Directory holding config.json and run/.

    Frozen builds resolve against sys.executable: under PyInstaller __file__
    points into a temp extraction directory that is deleted on exit, so
    config.json would be read from there (and so never seen) and the log would
    vanish with the process.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


HERE = base_dir()
RUN_DIR = HERE / "run"
LOG_FILE = RUN_DIR / "nowwatching.log"
CACHE_FILE = RUN_DIR / "metadata-cache.json"

# Optional siblings, imported by path so a frozen build finds them too. Either
# can be absent and the daemon still runs with whatever sources remain: no smtc
# means extension-only, no metadata means posters only from the extension.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
try:
    import smtc
except ImportError:
    smtc = None
try:
    import metadata as metadata_mod
except ImportError:
    metadata_mod = None

MIN_PUSH_INTERVAL = 4.0   # seconds between SET_ACTIVITY pushes
SEEK_EPSILON = 2.5        # position drift that counts as a seek, not playback
MAX_TEXT = 128            # Discord's limit on name/details/state

DEFAULT_CONFIG = {
    "client_id": "",
    "port": 6788,

    # auto      both sources, extension preferred when it is reporting
    # extension  browser only
    # smtc       Windows media session only, no extension needed
    "source": "auto",

    "activity_type": 3,
    "header_mode": "title",
    "timestamp_mode": "remaining",

    "show_poster": True,
    "show_site": False,
    "show_status_icon": False,
    "status_icon_keys": {"playing": "playing", "paused": "paused"},

    "paused_label": "Paused",

    # Poster art. The extension supplies one directly from the page; the SMTC
    # path has only a title, so it needs a lookup. TMDB covers film and TV and
    # wants a free key; without one, AniList still covers anime keylessly.
    "poster_lookup": True,
    "tmdb_api_key": "",

    # SMTC tuning. The duration floor is what stops a seven second reel
    # publishing as though it were something you sat down to watch.
    "smtc_min_duration": 60,
    "smtc_interval": 1.0,
    "smtc_ignore_apps": list(smtc.DEFAULT_IGNORE_APPS) if smtc else [],

    "idle_clear_seconds": 40,
    "idle_exit_seconds": 0,

    "allowed_origin_prefixes": [
        "chrome-extension://",
        "moz-extension://",
        "safari-web-extension://",
    ],
    "allow_missing_origin": True,

    "debug": False,
}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if CONFIG.get("debug"):
        print(line, file=sys.stderr, flush=True)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(HERE / "config.json", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"nowwatching: bad config.json ({e}); using defaults",
              file=sys.stderr)
    return cfg


CONFIG = load_config()


def use_utf8_stdout():
    """Titles carry non-ASCII that a cp1252 Windows console cannot encode.

    Only needed for the paths that print titles (--dry-run, --test). The real
    Discord path is already safe: json.dumps escapes to \\uXXXX first.

    line_buffering matters as much as the encoding. Python block-buffers stdout
    whenever it is not a terminal, so piping --test to a file or another process
    showed nothing at all until the run ended ninety seconds later, which reads
    exactly like a hang.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except (AttributeError, OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# pipe plumbing (shared with anicli-rpc)
# --------------------------------------------------------------------------

class _Eof:
    """Queue sentinel for 'the pipe died'.

    Must be a unique object, NOT a tuple or dict: Discord frames are
    themselves tuples, so any structural test would swallow real traffic.
    """

    def __repr__(self):
        return "<EOF>"


EOF = _Eof()


def bytes_available(fh):
    """How many bytes can be read right now without blocking.

    This exists because of a hard Windows constraint. A named-pipe handle
    opened synchronously (which is all plain open() can do) serialises its
    operations. Once a thread parks in a blocking ReadFile, a WriteFile issued
    on that same handle queues behind it and never runs, so the daemon would
    connect to Discord and then be unable to send anything, and Discord would
    close the connection with "Handshake timeout" having received nothing.

    Peeking first, and only ever reading bytes already buffered, keeps the
    handle idle so writes always get through.
    """
    if os.name != "nt":
        # FileIO.read(n) issues a single read() and returns whatever is ready,
        # so an upper bound is enough here; no need for an exact count.
        import select
        return 4096 if select.select([fh], [], [], 0)[0] else 0
    import ctypes
    import msvcrt
    avail = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.PeekNamedPipe(
        ctypes.c_void_p(msvcrt.get_osfhandle(fh.fileno())),
        None, 0, None, ctypes.byref(avail), None)
    if not ok:
        raise OSError("PeekNamedPipe failed (pipe closed)")
    return avail.value


class PipeReader(threading.Thread):
    """Non-blocking pipe drain on its own thread.

    `parse(buf) -> (messages, remainder)` pulls whole messages out of the
    accumulated buffer. Messages land on `out`; death lands as the EOF
    sentinel. The thread never issues a read larger than what is already
    available, so it never blocks a concurrent write. See bytes_available().
    """

    def __init__(self, handle, parse, out, poll=0.05):
        super().__init__(daemon=True)
        self.handle, self.parse, self.out, self.poll = handle, parse, out, poll

    def run(self):
        buf = b""
        try:
            while True:
                n = bytes_available(self.handle)
                if not n:
                    time.sleep(self.poll)
                    continue
                chunk = self.handle.read(n)
                if not chunk:
                    break
                buf += chunk
                msgs, buf = self.parse(buf)
                for m in msgs:
                    self.out.put(m)
        except (OSError, ValueError):
            pass
        finally:
            self.out.put(EOF)


def discord_socket_paths():
    r"""Every place a Discord IPC endpoint might live, in probe order.

    Windows uses named pipes. Everywhere else it is a Unix domain socket under
    a runtime dir, and the Flatpak and Snap builds bury it one level deeper,
    which is the usual reason a Linux user sees "Discord not found" while
    Discord is plainly running.
    """
    if os.name == "nt":
        return [rf"\\.\pipe\discord-ipc-{i}" for i in range(10)]

    roots = []
    for var in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        val = os.environ.get(var)
        if val:
            roots.append(val)
    roots.append("/tmp")

    subdirs = (
        "",
        "app/com.discordapp.Discord",
        "app/com.discordapp.DiscordCanary",
        "snap.discord",
        "snap.discord-canary",
        ".flatpak/dev.vencord.Vesktop/xdg-run",
    )
    paths, seen = [], set()
    for root in roots:
        for sub in subdirs:
            for i in range(10):
                p = os.path.join(root, sub, f"discord-ipc-{i}")
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
    return paths


class DiscordRPC:
    """Minimal Discord IPC client: 8-byte header (op, len) then a JSON body."""

    OP_HANDSHAKE, OP_FRAME, OP_CLOSE = 0, 1, 2

    def __init__(self, client_id):
        self.client_id = str(client_id)
        self.fh = None
        self.q = queue.Queue()
        self.connected = False

    def _open(self, path):
        if os.name == "nt":
            return open(path, "r+b", buffering=0)
        # open() on a Unix socket fails with ENXIO, so POSIX needs a real
        # socket. socket.makefile gives back the same read/write/close surface
        # the rest of this class (and PipeReader) expects.
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(path)
        sock.settimeout(None)
        return sock.makefile("rwb", buffering=0)

    def connect(self):
        for path in discord_socket_paths():
            try:
                self.fh = self._open(path)
            except (OSError, ValueError):
                continue
            PipeReader(self.fh, self._parse, self.q).start()
            try:
                self._send(self.OP_HANDSHAKE,
                           {"v": 1, "client_id": self.client_id})
            except OSError:
                self.close()
                continue
            if self._await_ready():
                self.connected = True
                log(f"discord: connected via {path}")
                return True
            self.close()
        return False

    def _await_ready(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # Clamped: queue.get() raises ValueError on a negative timeout.
                item = self.q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if item is EOF:
                log("discord: pipe closed during handshake")
                return False
            op, payload = item
            if op == self.OP_CLOSE:
                log(f"discord: refused handshake: {payload}")
                return False
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if data.get("evt") == "READY":
                return True
        log("discord: no READY before timeout")
        return False

    @staticmethod
    def _parse(buf):
        msgs = []
        while len(buf) >= 8:
            op, ln = struct.unpack("<II", buf[:8])
            if len(buf) < 8 + ln:
                break                       # body still in flight
            msgs.append((op, buf[8:8 + ln].decode("utf-8", "replace")))
            buf = buf[8 + ln:]
        return msgs, buf

    def _send(self, op, obj):
        data = json.dumps(obj).encode()
        try:
            self.fh.write(struct.pack("<II", op, len(data)) + data)
        except (OSError, AttributeError):
            self.connected = False
            raise

    def drain(self):
        """Must be called: an unread pipe eventually blocks the writer."""
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            if item is EOF:
                self.connected = False
                log("discord: pipe closed")
                return
            op, payload = item
            if op == self.OP_CLOSE:
                self.connected = False
                log(f"discord: closed by client: {payload}")

    def set_activity(self, activity):
        self._send(self.OP_FRAME, {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        })

    def close(self):
        self.connected = False
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


class DryRunRPC:
    """Stand-in for DiscordRPC that prints instead of publishing.

    Lets the whole extension -> HTTP -> activity path be verified without a
    Discord application id, and runs happily alongside the real daemon.
    """

    connected = True

    def __init__(self):
        use_utf8_stdout()

    def connect(self):
        return True

    def drain(self):
        pass

    def set_activity(self, activity):
        print("--- SET_ACTIVITY ---", flush=True)
        print(json.dumps(activity, indent=2, ensure_ascii=False), flush=True)

    def close(self):
        pass


# --------------------------------------------------------------------------
# report intake
# --------------------------------------------------------------------------

def _text(v, limit=MAX_TEXT):
    if not isinstance(v, str):
        return ""
    return " ".join(v.split())[:limit]


def _num(v, lo, hi):
    # bool is an int subclass, so True would otherwise sail through as 1.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):     # NaN, +/-inf
        return None
    return v if lo <= v <= hi else None


def _int(v, lo, hi):
    n = _num(v, lo, hi)
    return int(n) if n is not None else None


def _url(v):
    """http(s) only. A data: or javascript: url must never reach Discord."""
    if not isinstance(v, str) or len(v) > 1024:
        return None
    return v if v.startswith(("https://", "http://")) else None


def normalise(raw):
    """Coerce an extension report into the shape build_activity expects.

    Returns None for anything unusable, which the caller treats as "nothing is
    playing" rather than as an error.
    """
    if not isinstance(raw, dict):
        return None
    # Two characters, not one: Discord drops a text field shorter than that, so
    # a single-character title would publish a card with no title on it at all.
    title = _text(raw.get("title"))
    if len(title) < 2:
        return None

    season = _int(raw.get("season"), 0, 99)
    episode = _int(raw.get("episode"), 0, 9999)
    kind = raw.get("kind") if raw.get("kind") in ("series", "movie") else "movie"
    # A season or episode number is stronger evidence than the page's own
    # og:type, which these sites frequently leave as "video.movie" sitewide.
    if season or episode:
        kind = "series"

    return {
        "kind": kind,
        "title": title,
        "season": season,
        "episode": episode,
        "episode_title": _text(raw.get("episode_title")),
        "year": _int(raw.get("year"), 1880, 2100),
        "site": _text(raw.get("site"), 64),
        "poster": _url(raw.get("poster")),
        "position": _num(raw.get("position"), 0, 86400),
        "duration": _num(raw.get("duration"), 0, 86400),
        "paused": bool(raw.get("paused")),
        "adapter": _text(raw.get("adapter"), 32),
        "quality": _text(raw.get("quality"), 16),
    }


class Inbox:
    """Hand-off point between the HTTP threads and the main loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._report = None
        self._at = 0.0

    def put(self, report):
        with self._lock:
            self._report, self._at = report, time.time()

    def clear(self):
        with self._lock:
            self._report, self._at = None, time.time()

    def get(self):
        with self._lock:
            return self._report, self._at


STATUS = {
    "version": VERSION,
    "discord": False,
    "watching": None,
    "source": None,          # which source the current presence came from
    "pushes": 0,
}


class Handler(BaseHTTPRequestHandler):
    """Localhost JSON endpoint the extension talks to.

    Bound to 127.0.0.1 only, so the exposure is other local processes rather
    than the network. The Origin check narrows that further: a browser always
    attaches Origin to a cross-origin fetch and a page cannot forge it, so an
    ordinary web page cannot drive this even though it can reach the port.
    A request with no Origin at all is not something page JavaScript can
    produce; set allow_missing_origin false to reject those too.
    """

    inbox = None
    server_version = f"nowwatching/{VERSION}"
    protocol_version = "HTTP/1.1"

    _origins_logged = set()

    def log_message(self, fmt, *args):
        if CONFIG.get("debug"):
            log("http: " + (fmt % args))

    def handle_one_request(self):
        """Swallow the client hanging up, which is routine here, not an error.

        A keep-alive connection spends most of its life blocked in readline()
        waiting for the next request, so a browser that goes away mid-wait
        raises there. An extension service worker is killed and respawned
        constantly, so without this every sleep cycle prints a traceback that
        looks like a crash and is not one.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                TimeoutError):
            self.close_connection = True

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",
                         self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        # end_headers() flushes, so the headers can fail on a dead socket just
        # as easily as the body can. The whole write is guarded as one unit.
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True    # client hung up mid-reply

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        # Log each distinct origin once. Guessing which of these branches the
        # browser actually takes is how you ship a check that never fires.
        if origin not in self._origins_logged:
            self._origins_logged.add(origin)
            log(f"http: first request from Origin={origin!r}")
        if origin is None:
            return bool(CONFIG.get("allow_missing_origin", True))
        prefixes = tuple(CONFIG.get("allowed_origin_prefixes") or ())
        return bool(prefixes) and origin.startswith(prefixes)

    def _body(self):
        """Read and parse the request body, leaving the socket at a boundary.

        Consuming the body is not optional even when the request is about to be
        refused. This is keep-alive: an unread body stays in the socket and the
        next request line is parsed starting from it, so the following POST dies
        as `Unsupported method ('{"title":...}POST')`. One rejected request
        would poison every request after it on that connection.
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return None
        if n <= 0:
            return None
        if n > 64 * 1024:
            # Refuse to read it, so an oversized body cannot be used to make us
            # allocate. Dropping the connection is what avoids the desync here.
            self.close_connection = True
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except (OSError, ValueError):
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # Checked on reads too, not just writes. /v1/status reports the title
        # currently playing, and _cors() echoes the caller's Origin back, so
        # without this any web page could poll this port and read your watch
        # history. That would undo the whole point of not publishing it.
        if not self._origin_ok():
            self._reply(403, {"error": "origin not allowed"})
            return
        if self.path.startswith("/v1/status"):
            self._reply(200, dict(STATUS))
        else:
            self._reply(404, {"error": "no such endpoint"})

    def do_POST(self):
        # Unconditionally first: see _body() on why refusing before reading
        # breaks every subsequent request on the connection.
        raw = self._body()

        if not self._origin_ok():
            self._reply(403, {"error": "origin not allowed"})
            return
        if self.path.startswith("/v1/clear"):
            self.inbox.clear()
            self._reply(200, {"ok": True})
            return
        if not self.path.startswith("/v1/presence"):
            self._reply(404, {"error": "no such endpoint"})
            return
        report = normalise(raw)
        if report is None:
            self.inbox.clear()
            self._reply(200, {"ok": True, "accepted": False})
            return
        self.inbox.put(report)
        self._reply(200, {"ok": True, "accepted": True,
                          "discord": STATUS["discord"]})


# --------------------------------------------------------------------------
# presence construction
# --------------------------------------------------------------------------

DOT = " \u00b7 "        # the separator Discord's own cards use


def _set(activity, key, value):
    """Assign a text field, or drop it.

    Discord rejects name/details/state shorter than two characters, and a
    rejected frame loses the whole activity, not just the one field.
    """
    value = (value or "").strip()
    if len(value) >= 2:
        activity[key] = value[:MAX_TEXT]


def build_activity(rep, cfg):
    """Turn one normalised report into a Discord activity payload."""
    title = rep["title"]
    paused = rep["paused"]

    if rep["kind"] == "series":
        bits = []
        season, episode = rep["season"], rep["episode"]
        if season and episode:
            bits.append(f"S{season}:E{episode}")
        elif episode:
            bits.append(f"Episode {episode}")
        elif season:
            bits.append(f"Season {season}")
        if rep["episode_title"]:
            bits.append(rep["episode_title"])
        what = DOT.join(bits) or "Series"
    else:
        bits = []
        if rep["year"]:
            bits.append(str(rep["year"]))
        bits.append("Movie")
        what = DOT.join(bits)

    # The site is collected but not published unless asked for: broadcasting
    # where you stream from is nobody else's business, and it is the one field
    # here that says more about you than about what you are watching.
    where = []
    if paused:
        where.append(cfg.get("paused_label") or "Paused")
    if rep["quality"]:
        where.append(rep["quality"])
    if cfg.get("show_site") and rep["site"]:
        where.append(f"on {rep['site']}")
    where = DOT.join(where)

    activity = {"type": int(cfg.get("activity_type", 3))}

    if cfg.get("header_mode", "title") == "title":
        # Verified working: `name` overrides the top line, so the header can be
        # the title itself and the two lines below carry the detail.
        _set(activity, "name", title)
        _set(activity, "details", what)
        _set(activity, "state", where)
    else:
        # Fallback layout for when the header is the Discord application's own
        # name. The title has to move down a line, which costs us `where`.
        _set(activity, "details", title)
        _set(activity, "state", DOT.join(x for x in (what, where) if x))

    # Discord animates timestamps client-side, so a paused card would keep
    # counting down on its own. Send them only while actually playing.
    mode = cfg.get("timestamp_mode", "remaining")
    pos, dur = rep["position"], rep["duration"]
    if mode != "off" and not paused and pos is not None:
        start = int(time.time() - pos)
        if mode == "remaining" and dur and dur > pos:
            activity["timestamps"] = {"start": start, "end": int(start + dur)}
        else:
            activity["timestamps"] = {"start": start}

    assets = {}
    if cfg.get("show_poster") and rep["poster"]:
        # An external url, not an uploaded dev-portal asset, so there is
        # nothing to configure on the Discord application itself.
        assets["large_image"] = rep["poster"]
        hover = title
        if rep["year"]:
            hover += f" ({rep['year']})"
        if rep["kind"] == "series" and rep["season"]:
            hover += f"{DOT}Season {rep['season']}"
        assets["large_text"] = hover[:MAX_TEXT]
    if cfg.get("show_status_icon"):
        keys = cfg.get("status_icon_keys") or {}
        key = keys.get("paused" if paused else "playing")
        if key:
            assets["small_image"] = key
            assets["small_text"] = "Paused" if paused else "Playing"
    if assets:
        activity["assets"] = assets

    return activity


def presence_key(activity):
    """The part of the presence that changes discretely.

    Deliberately excludes the start timestamp; see start_changed().
    """
    ts = activity.get("timestamps") or {}
    assets = activity.get("assets") or {}
    return (
        activity.get("name"),
        activity.get("details"),
        activity.get("state"),
        activity.get("type"),
        bool(ts.get("end")),
        assets.get("large_image"),
        assets.get("small_image"),
    )


def start_changed(new, old):
    """Did playback actually jump, as opposed to merely advancing?

    start is derived as (now - position), so it holds steady during normal
    playback and moves only on a seek. It still jitters as the reported
    position lags, so it is compared against the last published value with a
    tolerance. Bucketing instead (int(start / epsilon)) looks equivalent but is
    not: values sitting near a bucket edge flip back and forth on that jitter
    and republish every few seconds, which walks straight into Discord's rate
    limit.
    """
    if (new is None) != (old is None):
        return True
    if new is None:
        return False
    return abs(new - old) > SEEK_EPSILON


def describe(activity):
    """One-line log form: what a human would read off the card."""
    head = activity.get("name") or activity.get("details") or "?"
    tail = activity.get("state") if activity.get("name") else None
    parts = [head]
    if activity.get("name") and activity.get("details"):
        parts.append(activity["details"])
    if tail:
        parts.append(tail)
    return " | ".join(parts)


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

SAMPLE_REPORT = {
    "kind": "series",
    "title": "Breaking Bad",
    "season": 5,
    "episode": 14,
    "episode_title": "Ozymandias",
    "year": 2008,
    "site": "example.tv",
    "poster": None,
    "position": 20 * 60,
    "duration": 48 * 60,
    "paused": False,
    "adapter": "--test",
    "quality": "1080p",
}


def run_test(cfg, seconds=90):
    """Publish one sample card, so the Discord half can be proven on its own.

    Answers the commonest support question ("is it my config or my adapter?")
    without a browser, an extension, or a real page in the loop.
    """
    use_utf8_stdout()
    if not cfg.get("client_id"):
        print("nowwatching: config.json has no client_id; see the README")
        return 2
    activity = build_activity(normalise(dict(SAMPLE_REPORT)), cfg)
    rpc = DiscordRPC(cfg["client_id"])
    if not rpc.connect():
        print("nowwatching: could not reach Discord. Is the desktop app running?")
        return 1
    rpc.set_activity(activity)
    print(json.dumps(activity, indent=2, ensure_ascii=False))
    print(f"\nPublished for {seconds}s. Your profile should read:")
    print(f"  Watching {activity.get('name') or '(application name)'}")
    print(f"  {activity.get('details', '')}")
    print(f"  {activity.get('state', '')}")
    print("\nNo poster in this sample; real pages supply one. Ctrl+C to stop.")
    deadline = time.time() + seconds
    try:
        while time.time() < deadline and rpc.connected:
            rpc.drain()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    try:
        rpc.set_activity(None)
    except OSError:
        pass
    rpc.close()
    return 0


def main(argv):
    cfg = CONFIG
    dry_run = "--dry-run" in argv

    if "--port" in argv:
        try:
            cfg["port"] = int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            print("nowwatching: --port needs a number", file=sys.stderr)
            return 2

    if not dry_run and not cfg.get("client_id"):
        log("FATAL: config.json has no client_id; see README.md")
        print("nowwatching: config.json has no client_id; see README.md",
              file=sys.stderr)
        return 2

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    inbox = Inbox()
    Handler.inbox = inbox

    port = int(cfg.get("port", 6788))
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        # Almost always a second daemon, which is why there is no separate
        # single-instance lock: the port is the lock.
        log(f"FATAL: cannot bind 127.0.0.1:{port} ({e})")
        print(f"nowwatching: port {port} is busy. Another copy is probably "
              f"already running.", file=sys.stderr)
        return 1
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    log(f"daemon: start on 127.0.0.1:{port}" + (" (dry-run)" if dry_run else ""))
    if dry_run:
        print(f"nowwatching {VERSION} dry-run, listening on 127.0.0.1:{port}",
              flush=True)

    source = str(cfg.get("source", "auto")).lower()

    smtc_src = None
    if smtc is not None and source in ("auto", "smtc"):
        smtc_src = smtc.SmtcSource(
            log,
            min_duration=cfg.get("smtc_min_duration", 60),
            ignore_apps=cfg.get("smtc_ignore_apps"),
            interval=cfg.get("smtc_interval", 1.0),
        )
        if not smtc_src.start():
            smtc_src = None
            if source == "smtc":
                log("FATAL: source is 'smtc' but the media session is unavailable")
                print("nowwatching: SMTC unavailable (Windows only). "
                      "Set source to 'extension' or 'auto'.", file=sys.stderr)
                return 1

    meta = None
    if metadata_mod is not None and cfg.get("poster_lookup", True):
        meta = metadata_mod.Metadata(log, CACHE_FILE,
                                     tmdb_key=cfg.get("tmdb_api_key", ""))

    rpc = DryRunRPC() if dry_run else DiscordRPC(cfg["client_id"])
    last_key = None
    last_start = None
    last_rev = None           # identifies the report we last built from
    pending = None
    last_push = 0.0
    idle_since = time.time()
    idle_clear = float(cfg.get("idle_clear_seconds", 40))
    idle_exit = float(cfg.get("idle_exit_seconds", 0))

    try:
        while True:
            time.sleep(0.4)
            if rpc.connected:
                rpc.drain()
            STATUS["discord"] = bool(rpc.connected)

            rep, at = inbox.get()
            # A live tab heartbeats, so silence means the tab is gone and the
            # browser never got to send an explicit clear (crash, kill, sleep).
            if rep is not None and idle_clear > 0 and time.time() - at > idle_clear:
                log("presence: report went stale, clearing")
                inbox.clear()
                rep = None

            if rep is not None:
                # The extension wins whenever it is reporting. It has the page's
                # own poster, and a url to take the season and episode from,
                # which SMTC cannot offer at all.
                rev = ("ext", at)
            elif smtc_src is not None and source != "extension":
                snap = smtc_src.snapshot()
                rep = normalise(snap) if snap else None
                rev = ("smtc", smtc_src.revision()) if rep is not None else None
            else:
                rev = None

            if rep is None:
                if last_key is not None:
                    if rpc.connected:
                        try:
                            rpc.set_activity(None)
                        except OSError:
                            rpc.close()
                    log("presence: cleared")
                    last_key = last_start = last_rev = pending = None
                    STATUS["watching"] = None
                    STATUS["source"] = None
                    idle_since = time.time()
                if idle_exit > 0 and time.time() - idle_since > idle_exit:
                    log("daemon: idle timeout, exiting")
                    return 0
                continue

            idle_since = time.time()

            # Only rebuild when a genuinely new report arrived.
            #
            # Rebuilding on every tick looks harmless but is not: `start` is
            # derived as (now - position), so while a report sits unchanged in
            # the inbox its derived start drifts forward with the wall clock,
            # crosses SEEK_EPSILON after a few seconds, and republishes as
            # though the user had seeked. A browser tab whose service worker
            # went to sleep would then push every few seconds and walk into
            # Discord's rate limit.
            #
            # Nothing is lost by skipping: Discord animates the countdown
            # client-side, so an unchanged report needs no traffic at all.
            #
            # The SMTC path is safe to rebuild once per helper line because it
            # extrapolates position from the moment it was measured, so its
            # derived start does not drift between lines either.
            if rev != last_rev:
                last_rev = rev
                # Fill in a poster the source could not supply. Answers from
                # cache and schedules the lookup in the background, so this
                # never blocks; the art lands on a later push.
                if meta is not None and not rep.get("poster"):
                    found = meta.poster_for(rep["title"], rep["kind"],
                                            rep.get("year"))
                    if found:
                        rep = dict(rep, poster=found)
                activity = build_activity(rep, cfg)
                key = presence_key(activity)
                start = (activity.get("timestamps") or {}).get("start")
                if key != last_key or start_changed(start, last_start):
                    pending = activity
                    last_key, last_start = key, start

            if pending is not None and time.time() - last_push >= MIN_PUSH_INTERVAL:
                if not rpc.connected and not rpc.connect():
                    time.sleep(2.0)         # Discord closed; retry later
                    continue
                try:
                    rpc.set_activity(pending)
                    log(f"presence: {describe(pending)}  [{rep.get('adapter')}]")
                    STATUS["watching"] = describe(pending)
                    STATUS["source"] = rep.get("adapter") or None
                    STATUS["pushes"] += 1
                    last_push = time.time()
                    pending = None
                except OSError:
                    rpc.close()
    except KeyboardInterrupt:
        pass
    finally:
        if smtc_src is not None:
            smtc_src.stop()
        if rpc.connected:
            try:
                rpc.set_activity(None)
            except OSError:
                pass
        rpc.close()
        log("daemon: stop")
    return 0


USAGE = f"""nowwatching {VERSION} - Discord Rich Presence for what you are watching

  python nowwatching.py              run the bridge (this is the normal case)
  python nowwatching.py --test       publish one sample card, no browser needed
  python nowwatching.py --dry-run    print payloads, never contacting Discord
  python nowwatching.py --port 6789  listen somewhere else

Two sources feed it, and `source` in config.json picks between them:

  smtc       the Windows media session. Nothing to install: play anything in
             any browser (or VLC, or any SMTC app) and it appears. Title
             quality is whatever the site reports, which some sites do badly.
  extension  the browser extension in extension/. Needs loading and a
             per-site grant, and in exchange gets the page's own poster and
             reads the season and episode from the url.
  auto       both, preferring the extension whenever it is reporting.

Needs the Discord desktop app running. Standard library only, nothing to
pip install.
"""


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(USAGE)
        sys.exit(0)
    if "--test" in args:
        sys.exit(run_test(CONFIG))
    sys.exit(main(args))
