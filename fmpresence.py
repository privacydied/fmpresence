#!/usr/bin/env python3
"""fmpresence: what you play on Plex, Pocket Casts or Last.fm as a Discord Rich Presence.

Subcommands:
  run         poll the sources and update Discord (default)
  doctor      check every part of the setup and print what is wrong
  pause       hide the presence until resume (bind it to a hotkey)
  resume      show the presence again
  toggle      pause if running, resume if paused
  install     write the systemd --user unit + env template, enable the service
  uninstall   disable and remove the unit
  serve-art   small signed image proxy for Plex covers (run it next to Plex)

serve-art uses only the standard library and runs on Python 3.8+.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("fmpresence")

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
ITUNES_API = "https://itunes.apple.com/search"
POCKETCASTS_API = "https://api.pocketcasts.com"
# Pocket Casts saves the position about every 10 s while it plays; no change for this long = paused
POCKETCASTS_IDLE = 40
# Last.fm's grey-star placeholder when a track has no art
LASTFM_PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f"
CLEAR_GRACE = 8  # seconds of "nothing playing" before the presence is cleared
THUMB_RE = re.compile(r"^/library/metadata/\d+/(thumb|art)/\d+$")

_XDG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CONFIG_DIR = _XDG / "fmpresence"
ENV_FILE = CONFIG_DIR / "env"
UNIT_DIR = _XDG / "systemd" / "user"
UNIT_FILE = UNIT_DIR / "fmpresence.service"
# per-boot pause flag: a reboot always resumes
PAUSE_FILE = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/fmpresence-{os.getuid()}") / "fmpresence.paused"


# ---------------------------------------------------------------- helpers

def norm(s: str) -> str:
    """Loose form for matching: no accents, case, punctuation, bracketed extras or 'feat.' tails."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def split_artists(s: str) -> str:
    # Plex joins multiple artists with ";"
    return ", ".join(a.strip() for a in (s or "").split(";") if a.strip())


def lastfm_url(artist: str, title: str) -> str:
    q = urllib.parse.quote_plus
    return f"https://www.last.fm/music/{q(artist)}/_/{q(title)}"


def lastfm_artist_url(artist: str) -> str:
    return "https://www.last.fm/music/" + urllib.parse.quote_plus(artist.split(",")[0].strip())


def lastfm_album_url(artist: str, album: str) -> str:
    q = urllib.parse.quote_plus
    return f"https://www.last.fm/music/{q(artist.split(',')[0].strip())}/{q(album)}" if album else ""


def spotify_search_url(artist: str, title: str) -> str:
    return "https://open.spotify.com/search/" + urllib.parse.quote(f"{artist} {title}")


def get_json(url: str, headers: dict | None = None, timeout: float = 10) -> dict:
    h = {"User-Agent": "fmpresence/1.0", "Accept": "application/json"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return json.load(r)


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _rules(v: str) -> set:
    # "|" separated, because artist and album names often contain commas
    return {norm(x) for x in v.split("|") if norm(x)}


class Cache:
    """Memo for network lookups. Failures are cached too, so a miss costs one call.

    ttl: seconds before the value is fetched again (None = keep for the process lifetime).
    """

    def __init__(self, size: int = 2000) -> None:
        self.size = size
        self.data: dict = {}

    def get(self, key: tuple, fn, ttl: float | None = None):
        hit = self.data.get(key)
        if hit is None or (ttl is not None and time.time() - hit[1] > ttl):
            if len(self.data) >= self.size:
                self.data.clear()
            try:
                value = fn()
            except Exception as e:
                log.debug("lookup %s failed: %s", key, e)
                value = None
            hit = self.data[key] = (value, time.time())
        return hit[0]

    def drop(self, key: tuple) -> None:
        self.data.pop(key, None)


CACHE = Cache()


# ---------------------------------------------------------------- config

class Config:
    def __init__(self, env=None) -> None:
        e = os.environ if env is None else env

        def g(k: str, d: str = "") -> str:
            return (e.get(k) or d).strip()

        self.discord_client_id = g("DISCORD_CLIENT_ID")
        self.pocketcasts_email = g("POCKETCASTS_EMAIL")
        self.pocketcasts_password = g("POCKETCASTS_PASSWORD")
        self.lastfm_user = g("LASTFM_USER")
        self.lastfm_api_key = g("LASTFM_API_KEY")
        self.plex_url = g("PLEX_URL").rstrip("/")
        self.plex_token = g("PLEX_TOKEN")
        self.plex_user = g("PLEX_USER")
        self.plex_websocket = _bool(g("PLEX_WEBSOCKET", "1"))
        self.poll = max(5, int(g("POLL_SECONDS", "15")))
        self.lastfm_poll = max(self.poll, int(g("LASTFM_POLL_SECONDS", "30")))
        self.show_paused = _bool(g("SHOW_PAUSED", "0"))
        self.show_video = _bool(g("SHOW_VIDEO", "0"))
        self.app_name = g("APP_NAME")
        self.status_display = g("STATUS_DISPLAY", "details").lower()
        self.itunes_country = g("ITUNES_COUNTRY", "us").lower()
        self.links = _bool(g("LINKS", "1"))
        self.clickable = _bool(g("CLICKABLE", "1"))
        self.playcount = _bool(g("PLAYCOUNT", "1"))
        self.art_base_url = g("ART_BASE_URL").rstrip("/")
        self.art_secret = g("ART_SECRET")
        self.art_listen = g("ART_LISTEN", "0.0.0.0:8765")
        self.spotify_icon = _bool(g("SPOTIFY_ICON", "0"))
        self.spotify_icon_url = g(
            "SPOTIFY_ICON_URL", "https://raw.githubusercontent.com/privacydied/fmpresence/main/assets/spotify-grey.png"
        )
        self.hide_artists = _rules(g("HIDE_ARTISTS"))
        self.hide_albums = _rules(g("HIDE_ALBUMS"))
        self.hide_tracks = _rules(g("HIDE_TRACKS"))
        self.hide_libraries = _rules(g("HIDE_LIBRARIES"))

    @property
    def use_plex(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    @property
    def use_pocketcasts(self) -> bool:
        return bool(self.pocketcasts_email and self.pocketcasts_password)

    @property
    def use_lastfm(self) -> bool:
        return bool(self.lastfm_user and self.lastfm_api_key)


# ---------------------------------------------------------------- art proxy signing

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def art_sign(secret: str, thumb: str) -> str:
    payload = _b64(thumb.encode())
    return payload + "." + _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()[:12])


def art_verify(secret: str, token: str):
    """Plex thumb path for a valid signed token, else None. Only library thumbs are allowed."""
    try:
        payload, sig = token.split(".", 1)
        good = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()[:12])
        if not hmac.compare_digest(good, sig):
            return None
        thumb = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    except Exception:
        return None
    return thumb if THUMB_RE.match(thumb) else None


def plex_art_url(cfg: Config, thumb: str) -> str:
    if not (cfg.art_base_url and cfg.art_secret and thumb and THUMB_RE.match(thumb)):
        return ""
    return f"{cfg.art_base_url}/a/{art_sign(cfg.art_secret, thumb)}.jpg"


# ---------------------------------------------------------------- Last.fm

def lastfm(cfg: Config, method: str, **params: str) -> dict:
    q = urllib.parse.urlencode({"method": method, "api_key": cfg.lastfm_api_key, "format": "json", **params})
    data = get_json(f"{LASTFM_API}?{q}")
    if "error" in data:
        raise RuntimeError(f"last.fm {method}: {data.get('message')} ({data['error']})")
    return data


def lastfm_image(images: list) -> str:
    art = next((i["#text"] for i in reversed(images or []) if i.get("#text")), "")
    return "" if LASTFM_PLACEHOLDER in art else art


def parse_lastfm_recent(data: dict):
    tracks = data["recenttracks"]["track"]
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not tracks or tracks[0].get("@attr", {}).get("nowplaying") != "true":
        return None
    t = tracks[0]
    artist, title = split_artists(t["artist"]["#text"]), t["name"]
    return {
        "kind": "music",
        "source": "lastfm",
        "id": f"lastfm:{artist}:{title}",
        "title": title,
        "artist": artist,
        "album_artist": artist,
        "album": t.get("album", {}).get("#text", ""),
        "library": "",
        "thumb": "",
        "art": lastfm_image(t.get("image", [])),
        "url": t.get("url") or lastfm_url(artist, title),
        "artist_raw": t["artist"]["#text"],
        "paused": False,
        "duration": 0,
        "offset": None,  # unknown: count from when we first saw it
    }


def lastfm_now_playing(cfg: Config):
    t = parse_lastfm_recent(lastfm(cfg, "user.getrecenttracks", user=cfg.lastfm_user, limit="1"))
    if t:
        def duration_ms() -> int:
            info = lastfm(cfg, "track.getInfo", artist=t["artist"], track=t["title"], autocorrect="1")
            return int(info["track"].get("duration") or 0)

        ms = CACHE.get(("dur", t["artist"], t["title"]), duration_ms)
        t["duration"] = (ms or 0) // 1000
    return t


def lastfm_album_art(cfg: Config, artist: str, album: str, title: str) -> str:
    if not cfg.lastfm_api_key:
        return ""

    def look() -> str:
        if album:
            try:
                info = lastfm(cfg, "album.getInfo", artist=artist, album=album, autocorrect="1")
                art = lastfm_image(info["album"].get("image", []))
                if art:
                    return art
            except Exception:
                pass
        t = lastfm(cfg, "track.getInfo", artist=artist, track=title, autocorrect="1")["track"]
        return lastfm_image(t.get("album", {}).get("image", []))

    return CACHE.get(("lfmart", artist, album, title), look) or ""


def lastfm_playcount(cfg: Config, t: dict) -> int:
    """How many times the Last.fm user has scrobbled this track (0 if unknown)."""
    if not (cfg.playcount and cfg.use_lastfm) or t["kind"] != "music":
        return 0

    def look() -> int:
        # Plex scrobbles the raw artist string ("A;B"); try that first, then the main artist
        for artist in dict.fromkeys(a for a in (t.get("artist_raw"), t["artist"].split(",")[0]) if a):
            info = lastfm(cfg, "track.getInfo", artist=artist, track=t["title"], username=cfg.lastfm_user, autocorrect="1")
            n = int(info.get("track", {}).get("userplaycount") or 0)
            if n:
                return n
        return 0

    return CACHE.get(("plays", t.get("artist_raw") or t["artist"], t["title"]), look, ttl=600) or 0


# ---------------------------------------------------------------- art proxy health

_art_up = None  # last known state, for logging changes only


def art_proxy_ok(cfg: Config) -> bool:
    """Is the public art proxy answering (through the CDN, so a TLS/route problem counts as down)?"""
    global _art_up

    def probe() -> bool:
        req = urllib.request.Request(cfg.art_base_url + "/health", headers={"User-Agent": "fmpresence/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200 and r.read(16).strip() == b"ok"

    # re-check every 5 min while up, every minute while down
    ok = bool(CACHE.get(("arthealth", cfg.art_base_url), probe, ttl=300 if _art_up else 60))
    if ok != _art_up:
        if ok:
            log.info("art proxy %s is up", cfg.art_base_url)
        else:
            log.warning("art proxy %s is down; using iTunes/Last.fm art", cfg.art_base_url)
        _art_up = ok
    return ok


# ---------------------------------------------------------------- iTunes (art + song.link)

def itunes_pick(results: list, artist: str, title: str, album: str = ""):
    """Best confident match from an iTunes song search, or None. Wrong links are worse than none."""
    na, nt, nb = norm(artist.split(",")[0]), norm(title), norm(album)
    best, best_score = None, 0
    for r in results:
        rt, ra, rb = norm(r.get("trackName", "")), norm(r.get("artistName", "")), norm(r.get("collectionName", ""))
        if not (rt and ra and nt and na):
            continue
        if not (rt == nt or rt.startswith(nt + " ") or nt.startswith(rt + " ")):
            continue
        if not (na in ra or ra in na):
            continue
        score = 2 + (rt == nt) + (2 if nb and rb == nb else 0)
        if score > best_score:
            best, best_score = r, score
    return best


def itunes_lookup(cfg: Config, artist: str, title: str, album: str):
    def look():
        q = urllib.parse.urlencode(
            {"term": f"{artist.split(',')[0]} {title}", "entity": "song", "limit": "15", "country": cfg.itunes_country}
        )
        r = itunes_pick(get_json(f"{ITUNES_API}?{q}").get("results", []), artist, title, album)
        if not r:
            return None
        return {
            "art": (r.get("artworkUrl100") or "").replace("100x100bb", "600x600bb"),
            "link": f"https://song.link/i/{r['trackId']}" if r.get("trackId") else "",
        }

    return CACHE.get(("itunes", norm(artist), norm(title), norm(album)), look)


# ---------------------------------------------------------------- Plex

def plex_owner(cfg: Config) -> str:
    """Token owner's Plex username (sessions report User.title as the username, not the display name)."""
    d = get_json("https://plex.tv/api/v2/user", {"X-Plex-Token": cfg.plex_token, "X-Plex-Client-Identifier": "fmpresence"})
    return d.get("username") or d.get("title") or ""


def parse_plex_sessions(data: dict, user: str, show_video: bool = False):
    kinds = {"track"} | ({"movie", "episode"} if show_video else set())
    sessions = [
        m for m in data.get("MediaContainer", {}).get("Metadata") or []
        if m.get("type") in kinds and (not user or m.get("User", {}).get("title", "").lower() == user.lower())
    ]
    if not sessions:
        return None
    # prefer an actively playing session, and music over video
    sessions.sort(key=lambda m: (m.get("Player", {}).get("state") != "playing", m.get("type") != "track"))
    m = sessions[0]
    base = {
        "source": "plex",
        "id": f"plex:{m.get('ratingKey')}:{m.get('sessionKey')}",
        "library": m.get("librarySectionTitle", ""),
        "paused": m.get("Player", {}).get("state") != "playing",
        "duration": int(m.get("duration") or 0) // 1000,
        "offset": int(m.get("viewOffset") or 0) / 1000,
        "art": "",
    }
    if m["type"] == "track":
        album_artist = split_artists(m.get("grandparentTitle", ""))
        artist = split_artists(m.get("originalTitle", "")) or album_artist
        title = m.get("title", "")
        return dict(base, kind="music", title=title, artist=artist, album_artist=album_artist or artist,
                    artist_raw=m.get("originalTitle") or m.get("grandparentTitle") or "",
                    album=m.get("parentTitle", ""), thumb=m.get("parentThumb") or m.get("thumb") or "",
                    url=lastfm_url(artist, title))
    if m["type"] == "episode":
        s, e = m.get("parentIndex"), m.get("index")
        ep = f"S{int(s):02d}E{int(e):02d}" if s is not None and e is not None else ""
        return dict(base, kind="video", title=m.get("grandparentTitle", ""),
                    artist=" · ".join(x for x in (ep, m.get("title", "")) if x),
                    album_artist="", album="", thumb=m.get("grandparentThumb") or m.get("thumb") or "", url="")
    year = m.get("year")
    directors = ", ".join(d.get("tag", "") for d in (m.get("Director") or [])[:2])
    return dict(base, kind="video", title=m.get("title", "") + (f" ({year})" if year else ""),
                artist=directors or "Movie", album_artist="", album="", thumb=m.get("thumb") or "", url="")


def plex_now_playing(cfg: Config, user: str):
    data = get_json(f"{cfg.plex_url}/status/sessions", {"X-Plex-Token": cfg.plex_token})
    return parse_plex_sessions(data, user, cfg.show_video)


class PlexWatcher(threading.Thread):
    """Plex websocket: wakes the main loop on every playback notification."""

    def __init__(self, cfg: Config, wake: threading.Event) -> None:
        super().__init__(daemon=True, name="plex-ws")
        self.cfg, self.wake = cfg, wake
        self.connected = False

    def run(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            log.warning("websockets not installed; polling only")
            return
        url = re.sub(r"^http", "ws", self.cfg.plex_url) + "/:/websockets/notifications"
        backoff = 5
        while True:
            try:
                with connect(url, additional_headers={"X-Plex-Token": self.cfg.plex_token}, open_timeout=10) as ws:
                    self.connected, backoff = True, 5
                    log.info("plex websocket connected")
                    for msg in ws:
                        text = msg if isinstance(msg, str) else msg.decode("utf-8", "ignore")
                        if '"playing"' in text:
                            self.wake.set()
            except Exception as e:
                log.debug("plex websocket: %s", e)
            if self.connected:
                log.info("plex websocket lost; polling until it reconnects")
            self.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def warm_art(cfg: Config, url: str):
    """Fetch a proxy cover once, so the CDN has it before Discord asks.

    A cold request makes Plex resize the image first; Discord can give up before that finishes.
    Returns the fetch thread for a new cover (the caller waits briefly), else None.
    """
    if not (cfg.art_base_url and url.startswith(cfg.art_base_url + "/a/")) or ("warm", url) in CACHE.data:
        return None
    CACHE.data[("warm", url)] = (True, time.time())

    def fetch() -> None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fmpresence/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            log.debug("warmed %s", url)
        except Exception as e:
            log.debug("warming %s failed: %s", url, e)

    th = threading.Thread(target=fetch, daemon=True, name="warm-art")
    th.start()
    return th


# ---------------------------------------------------------------- Pocket Casts (unofficial API)

def pocketcasts_track(e: dict, playing: bool, offset: float) -> dict:
    uuid, pod = e.get("uuid", ""), e.get("podcastUuid", "")
    return {
        "kind": "podcast",
        "source": "pocketcasts",
        "id": f"pocketcasts:{uuid}",
        "title": e.get("title", ""),
        "artist": e.get("podcastTitle", "") or e.get("author", ""),
        "album_artist": "",
        "album": e.get("author", "") if e.get("author") != e.get("podcastTitle") else "",
        "library": "",
        "thumb": "",
        "art": f"https://static.pocketcasts.com/discover/images/400/{pod}.jpg" if pod else "",
        "url": f"https://pca.st/episode/{uuid}" if uuid else "",
        "url_label": "Open in Pocket Casts",
        "artist_url": f"https://pca.st/podcast/{pod}" if pod else "",
        "link": "",
        "paused": not playing,
        "duration": int(e.get("duration") or 0),
        "offset": offset,
        # positions arrive every ~10 s, so allow more jitter before moving the progress bar
        "drift": 15,
    }


class PocketCasts:
    """Watches the most recent in-progress episode. Playing = its saved position keeps moving.

    The API has no "now playing" flag. While an episode plays, the apps save the position about
    every 10 s; when it pauses, the saves stop. An episode that jumps to the top of the list
    while we are watching was just started.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.token = ""
        self.token_until = 0.0
        self.last_uuid = None
        self.last_pos = None
        self.moved_at = 0.0  # when we last saw the position change (or a new episode start)

    def login(self) -> None:
        body = {"email": self.cfg.pocketcasts_email, "password": self.cfg.pocketcasts_password, "scope": "webplayer"}
        d = self._post("/user/login_pocket_casts", body, auth=False)
        self.token = d.get("accessToken") or d.get("token") or ""
        if not self.token:
            raise RuntimeError("pocket casts login returned no token")
        self.token_until = time.time() + int(d.get("expiresIn") or 3600) - 120

    def _post(self, path: str, body: dict, auth: bool = True) -> dict:
        h = {"Content-Type": "application/json", "User-Agent": "fmpresence/1.0"}
        if auth:
            h["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(POCKETCASTS_API + path, data=json.dumps(body).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    def latest(self):
        if not self.token or time.time() > self.token_until:
            self.login()
        try:
            eps = self._post("/user/in_progress", {}).get("episodes") or []
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self.login()
            eps = self._post("/user/in_progress", {}).get("episodes") or []
        return eps[0] if eps else None

    def step(self, e, now: float):
        """Turn the latest episode into a track (paused or playing), or None."""
        if not e:
            self.last_uuid = self.last_pos = None
            return None
        uuid, pos = e.get("uuid"), int(e.get("playedUpTo") or 0)
        if uuid != self.last_uuid:
            # a different episode on top while we watch = it was just started; at startup we cannot tell
            self.moved_at = now if self.last_uuid is not None else 0.0
        elif pos != self.last_pos:
            self.moved_at = now
        self.last_uuid, self.last_pos = uuid, pos
        playing = bool(self.moved_at) and now - self.moved_at <= POCKETCASTS_IDLE
        offset = pos + (now - self.moved_at if playing else 0)
        return pocketcasts_track(e, playing, offset)

    def now_playing(self, now: float):
        return self.step(self.latest(), now)


# ---------------------------------------------------------------- enrich / filter

def hidden(cfg: Config, t: dict) -> bool:
    def hit(rules: set, *values: str, split: bool = False) -> bool:
        for v in values:
            parts = [v] + (v.split(",") if split else [])
            if any(norm(p) in rules for p in parts if norm(p)):
                return True
        return False

    return (
        hit(cfg.hide_artists, t.get("artist", ""), t.get("album_artist", ""), split=True)
        or hit(cfg.hide_albums, t.get("album", ""))
        or hit(cfg.hide_tracks, t.get("title", ""))
        or hit(cfg.hide_libraries, t.get("library", ""))
    )


def enrich(cfg: Config, t: dict) -> dict:
    """Fill art + link. Art order: own Plex cover (if the proxy is set) > iTunes > Last.fm."""
    t.setdefault("link", "")
    if t["source"] == "pocketcasts":
        t["plays"] = 0
        return t
    if t.get("thumb") and cfg.art_base_url and cfg.art_secret and art_proxy_ok(cfg):
        t["art"] = plex_art_url(cfg, t["thumb"]) or t.get("art", "")
    if t["kind"] != "music":
        return t
    if cfg.links or not t["art"]:
        it = itunes_lookup(cfg, t["artist"], t["title"], t["album"])
        if it:
            t["art"] = t["art"] or it["art"]
            if cfg.links:
                t["link"] = it["link"]
    if not t["art"] and t["source"] == "plex":
        t["art"] = lastfm_album_art(cfg, t["album_artist"], t["album"], t["title"])
    t["plays"] = lastfm_playcount(cfg, t)
    return t


# ---------------------------------------------------------------- presence state

def clip(s: str, n: int = 128) -> str:
    # Discord rejects fields shorter than 2 or longer than 128 chars
    s = (s or "").strip()
    if len(s) < 2:
        s = (s + "  ")[:2]
    return s if len(s) <= n else s[: n - 1] + "…"


def hover_text(t: dict):
    """Album-art tooltip: "Album · played 37×"."""
    parts = [t.get("album") or ""]
    if t.get("plays"):
        parts.append(f"played {t['plays']}×")
    text = " · ".join(p for p in parts if p)
    return clip(text) if text else None


class Tracker:
    """Turns successive now-playing snapshots into Discord payloads.

    Keeps the progress bar steady between polls, moves it on seeks, and restarts it
    when the same track is replayed.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.key = None
        self.started = 0.0

    def step(self, t, now: float):
        """Return (changed, payload). payload None means clear the presence."""
        if t is None or (t["paused"] and not self.cfg.show_paused) or hidden(self.cfg, t):
            changed = self.key is not None
            self.key = None
            return changed, None
        key = (t["id"], t["paused"])
        changed = key != self.key
        if t["offset"] is not None:
            s = now - t["offset"]
            # seek, replay or new track: the Plex offset disagrees with our clock
            if changed or abs(s - self.started) > t.get("drift", 4):
                self.started = s
        elif changed:
            self.started = now
        elif t["duration"] and now > self.started + t["duration"] + 15:
            self.started = now  # Last.fm: still "now playing" after the track ended = replay
        self.key = key
        return changed, self.payload(t)

    def payload(self, t: dict) -> dict:
        from pypresence.types import ActivityType, StatusDisplayType

        c = self.cfg
        video = t["kind"] == "video"
        displays = {"name": StatusDisplayType.NAME, "state": StatusDisplayType.STATE}
        display = displays.get(c.status_display, StatusDisplayType.DETAILS)
        kw: dict = dict(
            activity_type=ActivityType.WATCHING if video else ActivityType.LISTENING,
            status_display_type=display,
            details=clip(t["title"]),
            state=clip(t["artist"] + (" · paused" if t["paused"] else "")),
            large_image=t.get("art") or None,
            large_text=hover_text(t),
        )
        if c.clickable and not video:
            # title -> song.link (or Last.fm track), artist -> Last.fm artist, cover -> Last.fm album
            kw["details_url"] = t.get("link") or t.get("url") or None
            kw["state_url"] = t.get("artist_url") or (lastfm_artist_url(t["artist"]) if t.get("artist") else None)
            if t["source"] == "pocketcasts":
                album_url = t.get("artist_url", "")
            else:
                album_url = lastfm_album_url(t.get("album_artist") or t["artist"], t.get("album", ""))
            kw["large_url"] = album_url if (album_url and t.get("art")) else None
        buttons = []
        if t.get("link"):
            buttons.append({"label": "Listen", "url": t["link"]})
        if t.get("url"):
            buttons.append({"label": t.get("url_label") or "View on Last.fm", "url": t["url"]})
        if buttons:
            kw["buttons"] = buttons[:2]
        if c.spotify_icon and t["kind"] == "music":
            kw.update(small_image=c.spotify_icon_url, small_text="Find on Spotify",
                      small_url=spotify_search_url(t["artist"], t["title"]))
        if not t["paused"]:
            kw["start"] = int(self.started)
            if t["duration"]:
                kw["end"] = int(self.started + t["duration"])
        if c.app_name:
            kw["name"] = c.app_name
        elif t["source"] == "pocketcasts":
            kw["name"] = "Pocket Casts"  # "Listening to Pocket Casts" instead of the Discord app's name
        return {k: v for k, v in kw.items() if v is not None}


class RPC:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.rpc = None

    def ensure(self) -> bool:
        if self.rpc:
            return True
        try:
            from pypresence.presence import Presence

            rpc = Presence(self.client_id)
            rpc.connect()
            self.rpc = rpc
            log.info("connected to discord")
            return True
        except Exception as e:
            log.debug("discord not reachable: %s", e)
            return False

    def drop(self) -> None:
        if self.rpc:
            try:
                self.rpc.close()
            except Exception:
                pass
        self.rpc = None

    def call(self, fn: str, **kw) -> bool:
        if not self.ensure():
            return False
        try:
            getattr(self.rpc, fn)(**kw)
            return True
        except Exception as e:
            log.warning("discord %s failed, reconnecting: %s", fn, e)
            self.drop()
            return False


# ---------------------------------------------------------------- run

def run(cfg: Config) -> int:
    if not cfg.discord_client_id or not (cfg.use_plex or cfg.use_pocketcasts or cfg.use_lastfm):
        log.error("need DISCORD_CLIENT_ID plus at least one source: PLEX_URL+PLEX_TOKEN, "
                  "POCKETCASTS_EMAIL+POCKETCASTS_PASSWORD or LASTFM_USER+LASTFM_API_KEY (see %s)", ENV_FILE)
        return 2

    plex_user = cfg.plex_user
    if cfg.use_plex and not plex_user:
        try:
            plex_user = plex_owner(cfg)
        except Exception as e:
            log.warning("could not resolve Plex account (%s); showing any user's session", e)
    srcs = ([f"plex ({plex_user or 'any user'})"] if cfg.use_plex else []) + (
        ["pocket casts"] if cfg.use_pocketcasts else []) + ([f"last.fm ({cfg.lastfm_user})"] if cfg.use_lastfm else [])
    log.info("sources: %s | art proxy: %s", " -> ".join(srcs), cfg.art_base_url or "off")

    wake = threading.Event()
    watcher = None
    if cfg.use_plex and cfg.plex_websocket:
        watcher = PlexWatcher(cfg, wake)
        watcher.start()

    rpc = RPC(cfg.discord_client_id)
    tracker = Tracker(cfg)
    pocket = PocketCasts(cfg) if cfg.use_pocketcasts else None
    signal.signal(signal.SIGTERM, lambda *_: (rpc.call("clear"), rpc.drop(), sys.exit(0)))
    signal.signal(signal.SIGUSR1, lambda *_: wake.set())  # pause/resume: re-check now
    was_paused = False

    sent = None  # last payload Discord accepted (None = cleared)
    sent_at = 0.0
    lastfm_next = 0.0
    idle_since = None
    backoff = cfg.poll
    while True:
        now = time.time()
        track, errors, tried = None, [], 0
        if cfg.use_plex:
            tried += 1
            try:
                track = plex_now_playing(cfg, plex_user)
            except Exception as e:
                errors.append(f"plex: {e}")
        if pocket:
            # polled every loop: playing is only visible as a position that keeps moving
            tried += 1
            try:
                pc = pocket.now_playing(now)
            except Exception as e:
                errors.append(f"pocket casts: {e}")
                pc = None
            # priority: playing Plex > playing Pocket Casts > paused Plex > paused Pocket Casts
            if pc and (track is None or (track["paused"] and not pc["paused"])):
                track = pc
        # Last.fm only matters when nothing else is on, and it changes slowly
        if track is None and cfg.use_lastfm and now >= lastfm_next:
            tried += 1
            lastfm_next = now + cfg.lastfm_poll
            try:
                track = lastfm_now_playing(cfg)
            except Exception as e:
                errors.append(f"last.fm: {e}")
        for e in errors:
            log.warning("poll failed: %s", e)
        if errors and len(errors) == tried:
            backoff = min(backoff * 2, 300)
            time.sleep(backoff)
            continue
        backoff = cfg.poll

        paused = PAUSE_FILE.exists()
        if paused != was_paused:
            log.info("paused by user" if paused else "resumed")
            was_paused = paused
        if paused:
            track = None
            if sent is not None:
                idle_since = now - CLEAR_GRACE  # clear at once, no grace
        if track:
            track = enrich(cfg, track)
            warming = warm_art(cfg, track["art"]) if track.get("art") else None
            if warming:
                warming.join(3)  # new cover: give the CDN a head start, but never hold the update long
            if not track.get("art") and ("noart", track["id"]) not in CACHE.data:
                CACHE.data[("noart", track["id"])] = (True, now)
                log.info("no album art found for %s - %s", track["artist"], track["title"])
        changed, payload = tracker.step(track, now)

        if payload is not None and track is not None:
            # send on change, plus a 30 s keepalive (also notices a restarted Discord)
            if payload != sent or now - sent_at > 30:
                if rpc.call("update", **payload):
                    if changed or sent is None:
                        what = "paused" if track["paused"] else "now playing"
                        log.info("%s [%s]: %s - %s", what, track["source"], track["artist"], track["title"])
                    sent, sent_at = payload, now
            idle_since = None
        elif sent is not None:
            # Plex drops the session for a moment between tracks: wait before clearing
            idle_since = idle_since or now
            if now - idle_since >= CLEAR_GRACE and rpc.call("clear"):
                log.info("nothing playing, cleared")
                sent, idle_since = None, None

        # websocket up: Plex pushes changes, so a slow safety poll is enough
        timeout = 30 if (watcher and watcher.connected) else cfg.poll
        if pocket:
            timeout = min(timeout, 15)  # Pocket Casts has no push; poll it often enough to see pauses
        if idle_since is not None:
            timeout = min(timeout, max(1.0, idle_since + CLEAR_GRACE - now))
        if wake.wait(timeout):
            wake.clear()
            time.sleep(0.5)  # Plex sends notifications in bursts


# ---------------------------------------------------------------- serve-art

def serve_art(cfg: Config) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if not (cfg.plex_url and cfg.plex_token and cfg.art_secret):
        log.error("serve-art needs PLEX_URL, PLEX_TOKEN and ART_SECRET")
        return 2
    store: dict = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "fmpresence-art"

        def log_message(self, format, *args):  # noqa: A002 (base class name)
            log.debug("%s %s", self.address_string(), format % args)

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/health":
                # healthy = this proxy answers and can reach Plex
                try:
                    req = urllib.request.Request(f"{cfg.plex_url}/identity", headers={"X-Plex-Token": cfg.plex_token})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        ok = r.status == 200
                except Exception:
                    ok = False
                body = b"ok\n" if ok else b"plex unreachable\n"
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            m = re.match(r"^/a/([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)\.jpg$", self.path.split("?", 1)[0])
            thumb = art_verify(cfg.art_secret, m.group(1)) if m else None
            if not thumb:
                self.send_error(404)
                return
            with lock:
                img = store.get(thumb)
            if img is None:
                q = urllib.parse.urlencode({"width": "600", "height": "600", "minSize": "1", "upscale": "1", "url": thumb})
                req = urllib.request.Request(f"{cfg.plex_url}/photo/:/transcode?{q}", headers={"X-Plex-Token": cfg.plex_token})
                try:
                    with urllib.request.urlopen(req, timeout=15) as r:
                        img = r.read()
                except Exception as e:
                    log.warning("plex image %s: %s", thumb, e)
                    self.send_error(502)
                    return
                with lock:
                    if len(store) > 300:
                        store.clear()
                    store[thumb] = img
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "public, max-age=604800, immutable")
            self.end_headers()
            self.wfile.write(img)

    host, _, port = cfg.art_listen.rpartition(":")
    srv = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    log.info("serving signed Plex covers on %s", cfg.art_listen)
    srv.serve_forever()
    return 0


# ---------------------------------------------------------------- pause / resume

def set_paused(paused) -> int:
    """paused: True, False, or None to toggle. Signals the service so it reacts at once."""
    if paused is None:
        paused = not PAUSE_FILE.exists()
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if paused:
        PAUSE_FILE.touch()
    else:
        PAUSE_FILE.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "kill", "-s", "USR1", "fmpresence"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print("paused" if paused else "resumed")
    return 0


# ---------------------------------------------------------------- doctor

def doctor(cfg: Config) -> int:
    """Check every part of the setup. Exit 1 if anything failed."""
    results = []

    def check(name: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append(ok)
        mark = "\033[32mOK  \033[0m" if ok is True else ("\033[33mSKIP\033[0m" if ok is None else "\033[31mFAIL\033[0m")
        print(f"{mark} {name:<16} {detail}")

    def env_file():
        if not ENV_FILE.exists():
            return False, f"{ENV_FILE} missing (run: fmpresence install)"
        mode = ENV_FILE.stat().st_mode & 0o777
        return mode & 0o077 == 0, f"{ENV_FILE} mode {mode:o}" + ("" if mode & 0o077 == 0 else " (should be 600)")

    def config():
        if not cfg.discord_client_id:
            return False, "DISCORD_CLIENT_ID is empty"
        if not (cfg.use_plex or cfg.use_pocketcasts or cfg.use_lastfm):
            return False, "no source: set Plex, Pocket Casts and/or Last.fm variables"
        on = (("plex", cfg.use_plex), ("pocket casts", cfg.use_pocketcasts), ("last.fm", cfg.use_lastfm))
        return True, "sources: " + ", ".join(n for n, enabled in on if enabled)

    def discord():
        run_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        socks = sorted(str(p) for p in run_dir.glob("discord-ipc-*")) + sorted(
            str(p) for p in run_dir.glob("app/*/discord-ipc-*"))
        if not socks:
            return False, f"no discord-ipc socket in {run_dir} (is the Discord desktop app running?)"
        if not cfg.discord_client_id:
            return None, socks[0]
        from pypresence.presence import Presence

        rpc = Presence(cfg.discord_client_id)
        rpc.connect()
        rpc.close()
        return True, f"handshake ok via {socks[0]}"

    def plex():
        if not cfg.use_plex:
            return None, "not configured"
        ident = get_json(f"{cfg.plex_url}/identity", {"X-Plex-Token": cfg.plex_token})["MediaContainer"]
        user = cfg.plex_user or plex_owner(cfg)
        t = plex_now_playing(cfg, user)
        now = f"{'paused' if t['paused'] else 'playing'} {t['artist']} - {t['title']}" if t else "nothing playing"
        return True, f"server {ident.get('version')}, user {user}, {now}"

    def websocket():
        if not (cfg.use_plex and cfg.plex_websocket):
            return None, "off"
        from websockets.sync.client import connect

        url = re.sub(r"^http", "ws", cfg.plex_url) + "/:/websockets/notifications"
        with connect(url, additional_headers={"X-Plex-Token": cfg.plex_token}, open_timeout=8):
            pass
        return True, "connected"

    def pocketcasts_check():
        if not cfg.use_pocketcasts:
            return None, "not configured"
        e = PocketCasts(cfg).latest()
        if not e:
            return True, "logged in, no episode in progress"
        return True, f"logged in, latest: {e.get('podcastTitle')} - {e.get('title')} at {int(e.get('playedUpTo') or 0)} s"

    def lastfm_check():
        if not cfg.use_lastfm:
            return None, "not configured"
        u = lastfm(cfg, "user.getinfo", user=cfg.lastfm_user)["user"]
        return True, f"user {u.get('name')}, {int(u.get('playcount') or 0):,} scrobbles"

    def itunes():
        if not cfg.links:
            return None, "LINKS=0"
        n = get_json(f"{ITUNES_API}?term=radiohead+creep&entity=song&limit=1").get("resultCount", 0)
        return n > 0, "search ok" if n else "no results"

    def art():
        if not cfg.art_base_url:
            return None, "ART_BASE_URL not set (covers come from iTunes/Last.fm)"
        if not cfg.art_secret:
            return False, "ART_BASE_URL is set but ART_SECRET is empty"
        req = urllib.request.Request(cfg.art_base_url + "/health", headers={"User-Agent": "fmpresence/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read(64).decode(errors="ignore").strip()
        except urllib.error.HTTPError as e:
            hint = {526: "CDN rejects the origin TLS certificate", 502: "proxy not running?",
                    503: "proxy cannot reach Plex", 404: "route goes to the wrong backend"}.get(e.code, "")
            return False, f"/health -> HTTP {e.code} {hint}".strip()
        return body == "ok", f"/health -> {body}"

    def service():
        r = subprocess.run(["systemctl", "--user", "is-active", "fmpresence"], capture_output=True, text=True)
        e = subprocess.run(["systemctl", "--user", "is-enabled", "fmpresence"], capture_output=True, text=True)
        state, enabled = r.stdout.strip() or "unknown", e.stdout.strip() or "unknown"
        paused = " (paused by user)" if PAUSE_FILE.exists() else ""
        return state == "active", f"{state}, {enabled}{paused}"

    for name, fn in (("env file", env_file), ("config", config), ("discord", discord), ("plex", plex),
                     ("plex websocket", websocket), ("pocket casts", pocketcasts_check), ("last.fm", lastfm_check),
                     ("itunes", itunes), ("art proxy", art), ("service", service)):
        check(name, fn)
    return 1 if any(r is False for r in results) else 0


# ---------------------------------------------------------------- install

ENV_TEMPLATE = """\
# fmpresence config. Docs: https://github.com/privacydied/fmpresence
DISCORD_CLIENT_ID=
# Plex source (real-time). Token: Plex Web -> Get Info -> View XML -> X-Plex-Token
PLEX_URL=
PLEX_TOKEN=
# Last.fm: fallback source for other players, and album art
LASTFM_USER=
LASTFM_API_KEY=
POLL_SECONDS=15
# STATUS_DISPLAY=details   # member list shows: details (track), state (artist) or name (app)
# SHOW_PAUSED=1
# SHOW_VIDEO=1
# HIDE_ARTISTS=Artist One|Artist Two
# Pocket Casts (unofficial API, email login only)
# POCKETCASTS_EMAIL=
# POCKETCASTS_PASSWORD=
# ART_BASE_URL=https://art.example.com
# ART_SECRET=
"""

UNIT_TEMPLATE = """\
[Unit]
Description=fmpresence: Plex/Pocket Casts/Last.fm now playing -> Discord Rich Presence
After=graphical-session.target network-online.target

[Service]
EnvironmentFile=%h/.config/fmpresence/env
ExecStart={exec}
Restart=always
RestartSec=10
RestartPreventExitStatus=2
NoNewPrivileges=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
UMask=0077

[Install]
WantedBy=default.target
"""


def install() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.write_text(ENV_TEMPLATE)
        print(f"wrote {ENV_FILE} - fill it in")
    ENV_FILE.chmod(0o600)
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = " ".join(shlex.quote(x) for x in (sys.executable, "-m", "fmpresence", "run"))
    UNIT_FILE.write_text(UNIT_TEMPLATE.format(exec=cmd))
    print(f"wrote {UNIT_FILE}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "fmpresence"], check=True)
    subprocess.run(["systemctl", "--user", "restart", "fmpresence"], check=True)
    print("enabled. logs: journalctl --user -u fmpresence -f")
    return 0


def uninstall() -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", "fmpresence"], check=False)
    if UNIT_FILE.exists():
        UNIT_FILE.unlink()
        print(f"removed {UNIT_FILE}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"config kept at {ENV_FILE}")
    return 0


def load_env_file() -> dict:
    """os.environ plus the env file, for commands run outside systemd (doctor)."""
    env = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "run"
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd == "install":
        return install()
    if cmd == "uninstall":
        return uninstall()
    if cmd == "serve-art":
        return serve_art(Config())
    if cmd in ("pause", "resume", "toggle"):
        return set_paused({"pause": True, "resume": False, "toggle": None}[cmd])
    if cmd == "doctor":
        logging.getLogger().setLevel(logging.WARNING)
        return doctor(Config(load_env_file()))
    if cmd == "run":
        return run(Config())
    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
