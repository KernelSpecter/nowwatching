r"""Poster art lookup, cached on disk.

Needed because of an asymmetry between the two sources. The browser extension
scrapes the page's own og:image, which is exact and free. The Windows media
session hands over a WinRT byte stream instead, and Discord's large_image takes
a url, so the SMTC path has to find one by name.

Providers, in the order they are tried. Every one of these except TMDB needs no
key, no signup and no configuration, which is the point: the whole tool has to
work the moment it is run.

    TVmaze     series. No key. Measured 5/5 on test titles, all exact.
    AniList    anime. No key. Better cover art than the general databases.
    Wikipedia  film and series. No key. Measured 5/5 on films.
    TMDB       everything, and better than all of the above, but wants a key.
               Tried first when one is configured.

Two candidates were measured and rejected rather than assumed:

* iTunes Search: 3 of 8 titles, one an outright wrong match ("The Bear" ->
  a bear documentary), every film missed. Region-dependent and unusable.
* Wikipedia unguarded: "Parasite" resolves to "Parasitism", the biology
  article. Its summary carries a `description` field, so a match is accepted
  only when that description actually describes a film or a series. The guard
  turns a confidently wrong answer into an honest miss.

IMDb is absent because it has no free public API. TMDB carries IMDb ids for
every title, so nothing is lost by going through it.

Shipping one shared TMDB key for all users was considered and rejected: it
would sit in a public repo, all traffic would run under one account, and a
revocation would break the feature for everyone at once in an already-released
build. The keyless providers remove the need.

Lookups never block the presence loop. poster_for() answers from cache
immediately and schedules a fetch in the background; the poster appears on a
later push once it lands.
"""

import json
import re
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


def _norm(s):
    """Casefolded, punctuation-free form, for comparing two titles."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


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

    # TVmaze's own relevance score. Measured on a mixed set: real series scored
    # 1.19 to 1.61, while films that fuzzy-matched a show scored 0.64 to 0.86.
    # 1.0 sits in the gap.
    TVMAZE_MIN_SCORE = 1.0

    def _tvmaze(self, title, kind, year):
        """Series only, no key.

        /search/shows rather than /singlesearch/shows, because singlesearch
        returns its best fuzzy guess with no score and no way to reject it: it
        confidently answered "Blade Runner 2049" with the show "Blade Runner
        2099", and every film in a test set came back looking like a hit.
        """
        url = ("https://api.tvmaze.com/search/shows?"
               + urllib.parse.urlencode({"q": title}))
        payload, definitive = _get_json(url)
        if payload is None:
            return None, definitive
        if not isinstance(payload, list) or not payload:
            return None, True

        for row in payload[:5]:
            show = row.get("show") or {}
            score = row.get("score") or 0
            name = show.get("name") or ""
            art = (show.get("image") or {}).get("original")
            if not art:
                continue

            exact = _norm(name) == _norm(title)
            if score < self.TVMAZE_MIN_SCORE and not exact:
                continue

            premiered = show.get("premiered") or ""
            got_year = int(premiered[:4]) if premiered[:4].isdigit() else None
            # A show and a film can share a name: TVmaze is right that a 1980
            # series called "Oppenheimer" exists, it is just not the 2023 film.
            # The year is the only thing that separates them.
            if year and got_year and abs(got_year - year) > 2:
                continue

            return {
                "poster": art,
                "title": name or title,
                "year": got_year or year,
                "url": show.get("url"),
                "source": "tvmaze",
            }, True
        return None, True

    # A summary is only accepted when its own description says it is a film or a
    # show. Without this, "Parasite" resolves to "Parasitism" and the biology
    # article's illustration gets published as the poster.
    WIKI_OK = ("film", "television", "tv series", "anime", "miniseries",
               "series", "movie", "sitcom", "drama")

    def _wikipedia(self, title, kind, year):
        hints = ["film"] if kind == "movie" else ["TV series",
                                                 "anime television series"]
        for probe in [f"{title} ({h})" for h in hints] + [title]:
            slug = urllib.parse.quote(probe.replace(" ", "_"), safe="")
            payload, definitive = _get_json(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + slug)
            if payload is None:
                if not definitive:
                    return None, False       # network trouble, retry later
                continue
            if payload.get("type") == "disambiguation":
                continue
            blurb = " ".join([
                payload.get("description") or "",
                payload.get("extract") or "",
            ]).lower()
            if not any(k in blurb for k in self.WIKI_OK):
                continue
            art = ((payload.get("thumbnail") or {}).get("source")
                   or (payload.get("originalimage") or {}).get("source"))
            if not art:
                continue
            # Thumbnails come back around 320px wide. The width is in the path,
            # so asking for a bigger one is a rewrite rather than another call.
            # originalimage is deliberately not preferred: it can be many
            # megabytes, which Discord then has to fetch.
            art = re.sub(r"/\d{2,4}px-", "/500px-", art)
            return {
                "poster": art,
                "title": payload.get("title") or title,
                "year": year,
                "url": ((payload.get("content_urls") or {}).get("desktop")
                        or {}).get("page"),
                "source": "wikipedia",
            }, True
        return None, True

    def _anilist(self, title, year=None):
        """Anime only, no key. Cleanly 404s on anything that is not anime."""
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
        got_year = media.get("seasonYear")
        # Search here is fuzzy too: "Parasite" (the 2019 Korean film) matches an
        # unrelated anime of the same name. The year separates them.
        if year and got_year and abs(got_year - year) > 2:
            return None, True
        return {
            "poster": art,
            "title": titles.get("english") or titles.get("romaji") or title,
            "year": got_year,
            "url": media.get("siteUrl"),
            "source": "anilist",
        }, True

    # -- public -----------------------------------------------------------

    def _providers(self, kind):
        """Which providers to try, best first.

        TMDB goes first when a key exists because it beats the rest. Everything
        after it is keyless, so the default path needs no configuration.

        kind None means the source could not tell a film from a show, which is
        the common case when all it had was a title. The series-only providers
        are tried first in that case: they hold nothing but shows, so a hit is
        proof of what this is, and that is worth more than search order.
        """
        tmdb_tv = lambda t, k, y: self._tmdb(t, "series", y)     # noqa: E731
        tmdb_film = lambda t, k, y: self._tmdb(t, "movie", y)    # noqa: E731
        tvmaze = lambda t, k, y: self._tvmaze(t, "series", y)    # noqa: E731
        anilist = lambda t, k, y: self._anilist(t, y)            # noqa: E731
        wiki = lambda t, k, y: self._wikipedia(t, kind or "movie", y)  # noqa: E731

        # AniList sits early for a known series and last otherwise. Its search
        # matches on name alone, so "Parasite" (the 2019 film) hits an anime of
        # the same name; Wikipedia's description guard gets that right, so it
        # goes first when we do not already know this is a show. When we do,
        # anime is likely and AniList has the better art.
        if kind == "series":
            chain = [tvmaze, anilist, wiki]
            if self.tmdb_key:
                chain.insert(0, tmdb_tv)
        elif kind == "movie":
            chain = [wiki, anilist]
            if self.tmdb_key:
                chain.insert(0, tmdb_film)
        else:
            chain = [tvmaze, wiki, anilist]
            if self.tmdb_key:
                chain = [tmdb_tv, tvmaze, tmdb_film, wiki, anilist]
        return chain

    def _fetch(self, key, title, kind, year):
        info, definitive = None, True
        try:
            for call in self._providers(kind):
                info, definitive = call(title, kind, year)
                if info:
                    break
                if not definitive:
                    # Network trouble rather than a real miss. Stop here so the
                    # result is not cached as "no such title".
                    break
            if info is None and definitive and year and self.tmdb_key:
                # The year came out of a page title and is often wrong, or names
                # a different release. Worth one retry without it.
                info, definitive = self._tmdb(title, kind, None)
        except Exception as e:                      # noqa: BLE001
            self.log(f"metadata: lookup failed for {title!r}: {e}")
            definitive = False

        if info:
            # Which provider answered is itself evidence of what this is. TVmaze
            # and AniList only hold series, so a hit there settles a `kind` the
            # title alone could not: a show with no season or episode in its
            # title would otherwise be published as "Movie".
            if info["source"] in ("tvmaze", "anilist"):
                info["kind"] = "series"
            elif info["source"] == "wikipedia":
                info["kind"] = None      # the guard accepted either
            else:
                info["kind"] = None

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

    def info_for(self, title, kind, year):
        """Cached match for this title, or None. Schedules a lookup on a miss.

        Returns immediately either way. Nothing here waits on the network, so a
        slow or unreachable provider cannot stall presence updates. The answer
        carries `kind` when the provider that matched settles it.
        """
        if not self.enabled or not title:
            return None

        key = self._key(title, kind, year)
        with self._lock:
            if key in self._cache:
                return self._cache[key]          # may be None: a cached miss
            if key in self._inflight:
                return None
            self._inflight.add(key)

        threading.Thread(target=self._fetch,
                         args=(key, title, kind, year), daemon=True).start()
        return None

    def poster_for(self, title, kind, year):
        info = self.info_for(title, kind, year)
        return info.get("poster") if info else None


if __name__ == "__main__":
    # Manual check, no key needed: python metadata.py [tmdb_key]
    import sys
    from pathlib import Path

    key = sys.argv[1] if len(sys.argv) > 1 else ""
    md = Metadata(lambda m: print("  " + m, flush=True),
                  Path("run/metadata-probe.json"), tmdb_key=key)

    # kind None is the important column: it is what a title-only source gives,
    # and the provider that matches has to settle film vs series.
    CASES = [
        ("The Mentalist", None, 2008),
        ("Breaking Bad", None, None),
        ("Blade Runner 2049", None, 2017),
        ("Parasite", None, 2019),
        ("Frieren: Beyond Journey's End", None, None),
        ("Oppenheimer", None, 2023),
    ]
    for t, k, y in CASES:
        md.info_for(t, k, y)
    time.sleep(12)
    print()
    for t, k, y in CASES:
        info = md.info_for(t, k, y)
        if info:
            print(f"{t:34} {info['source']:10} kind={info.get('kind')} "
                  f"{(info.get('poster') or '')[:58]}")
        else:
            print(f"{t:34} (no match)")
