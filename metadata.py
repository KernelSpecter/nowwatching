r"""Poster art lookup, cached on disk.

Needed because of an asymmetry between the two sources. The browser extension
scrapes the page's own og:image, which is exact and free. The Windows media
session hands over a WinRT byte stream instead, and Discord's large_image takes
a url, so the SMTC path has to find one by name.

Provider notes, since the obvious candidates mostly do not work:

* IMDb has no free public API. TMDB is the practical stand-in and carries IMDb
  ids for every title, so nothing is lost by going through it.
* TMDB needs a free key. That is one config line, not per-site setup, and
  without it this whole module simply returns nothing.
* AniList needs no key at all but only covers anime. It is tried when there is
  no TMDB key, which makes anime work out of the box.
* The keyless iTunes Search API was measured at 3 of 8 titles, including one
  outright wrong match, so it is deliberately not used.

Lookups never block the presence loop. poster_for() answers from cache
immediately and schedules a fetch in the background; the poster appears on a
later push once it lands.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "nowwatching/0.1 (+https://github.com/KernelSpecter/nowwatching)"
TIMEOUT = 8

TMDB_IMAGE = "https://image.tmdb.org/t/p/w780"

ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    title { romaji english }
    coverImage { extraLarge large }
    siteUrl
    seasonYear
  }
}
"""


def _get_json(url, data=None, headers=None):
    """-> (payload, definitive). definitive=False means 'retry later'."""
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": UA, "Accept": "application/json",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp), True
    except urllib.error.HTTPError as e:
        # 401 is a bad key and 404 is a real miss: both are settled answers.
        # 429 and 5xx are not, and caching them would poison the cache.
        return None, e.code in (401, 403, 404)
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        return None, False


class Metadata:
    """Title -> poster url, with a disk cache so each title is looked up once."""

    def __init__(self, log, cache_file, tmdb_key="", enabled=True):
        self.log = log
        self.cache_file = cache_file
        self.tmdb_key = (tmdb_key or "").strip()
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._inflight = set()
        self._cache = {}
        try:
            with open(cache_file, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._cache = loaded
        except (OSError, ValueError):
            pass

    # -- cache ------------------------------------------------------------

    @staticmethod
    def _key(title, kind, year):
        return f"{kind}|{(title or '').strip().lower()}|{year or ''}"

    def _save(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, indent=1)
            tmp.replace(self.cache_file)
        except OSError:
            pass

    # -- providers --------------------------------------------------------

    def _tmdb(self, title, kind, year):
        if not self.tmdb_key:
            return None, True
        params = {"api_key": self.tmdb_key, "query": title,
                  "include_adult": "false"}
        # search/tv and search/movie beat search/multi here: we already know
        # which we want, and multi mixes in people, whose profile photo would
        # otherwise be published as the poster.
        path = "tv" if kind == "series" else "movie"
        if year:
            params["first_air_date_year" if kind == "series" else "year"] = year
        url = (f"https://api.themoviedb.org/3/search/{path}?"
               + urllib.parse.urlencode(params))
        payload, definitive = _get_json(url)
        if payload is None:
            return None, definitive

        for item in (payload.get("results") or []):
            if not item.get("poster_path"):
                continue
            name = item.get("name") or item.get("title") or title
            date = item.get("first_air_date") or item.get("release_date") or ""
            return {
                "poster": TMDB_IMAGE + item["poster_path"],
                "title": name,
                "year": int(date[:4]) if date[:4].isdigit() else year,
                "url": f"https://www.themoviedb.org/{path}/{item.get('id')}",
                "source": "tmdb",
            }, True
        return None, True

    def _anilist(self, title):
        body = json.dumps({"query": ANILIST_QUERY,
                           "variables": {"search": title}}).encode()
        payload, definitive = _get_json(
            "https://graphql.anilist.co", data=body,
            headers={"Content-Type": "application/json"})
        if payload is None:
            return None, definitive
        media = (payload.get("data") or {}).get("Media")
        if not media:
            return None, True
        cover = media.get("coverImage") or {}
        art = cover.get("extraLarge") or cover.get("large")
        if not art:
            return None, True
        titles = media.get("title") or {}
        return {
            "poster": art,
            "title": titles.get("english") or titles.get("romaji") or title,
            "year": media.get("seasonYear"),
            "url": media.get("siteUrl"),
            "source": "anilist",
        }, True

    # -- public -----------------------------------------------------------

    def _fetch(self, key, title, kind, year):
        info, definitive = None, True
        try:
            if self.tmdb_key:
                info, definitive = self._tmdb(title, kind, year)
                if info is None and definitive and year:
                    # The year came from a page title and is often wrong or
                    # belongs to a different release. Worth one retry without it
                    # before giving up on the title entirely.
                    info, definitive = self._tmdb(title, kind, None)
            else:
                info, definitive = self._anilist(title)
        except Exception as e:                      # noqa: BLE001
            self.log(f"metadata: lookup failed for {title!r}: {e}")
            definitive = False

        with self._lock:
            self._inflight.discard(key)
            if info or definitive:
                # A settled miss is cached as None so it is not retried on every
                # single report for the rest of the session.
                self._cache[key] = info
                self._save()
        if info:
            self.log(f"metadata: {title!r} -> {info['source']} poster")
        elif definitive:
            self.log(f"metadata: no match for {title!r}")

    def poster_for(self, title, kind, year):
        """Cached poster url, or None. Schedules a lookup on a miss.

        Returns immediately either way. Nothing here waits on the network, so a
        slow or unreachable provider cannot stall presence updates.
        """
        if not self.enabled or not title:
            return None
        if not self.tmdb_key and kind == "movie":
            # AniList is anime-only, so a film with no TMDB key has no provider
            # that could answer. Skip the request rather than guarantee a miss.
            return None

        key = self._key(title, kind, year)
        with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                return entry.get("poster") if entry else None
            if key in self._inflight:
                return None
            self._inflight.add(key)

        threading.Thread(target=self._fetch,
                         args=(key, title, kind, year), daemon=True).start()
        return None


if __name__ == "__main__":
    # Manual check: python metadata.py [tmdb_key]
    import sys
    from pathlib import Path

    key = sys.argv[1] if len(sys.argv) > 1 else ""
    md = Metadata(lambda m: print(m, flush=True),
                  Path("run/metadata-cache.json"), tmdb_key=key)
    for t, k, y in [("Breaking Bad", "series", 2008),
                    ("Blade Runner 2049", "movie", 2017),
                    ("Frieren: Beyond Journey's End", "series", None)]:
        md.poster_for(t, k, y)
    time.sleep(6)
    for t, k, y in [("Breaking Bad", "series", 2008),
                    ("Blade Runner 2049", "movie", 2017),
                    ("Frieren: Beyond Journey's End", "series", None)]:
        print(f"{t}: {md.poster_for(t, k, y)}")
