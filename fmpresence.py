#!/usr/bin/env python3
"""fmpresence: what you play on Plex (or Last.fm) as a Discord Rich Presence.

Subcommands:
  run         poll the sources and update Discord (default)
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
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("fmpresence")

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
ITUNES_API = "https://itunes.apple.com/search"
# Last.fm's grey-star placeholder when a track has no art
LASTFM_PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f"
CLEAR_GRACE = 8  # seconds of "nothing playing" before the presence is cleared
THUMB_RE = re.compile(r"^/library/metadata/\d+/(thumb|art)/\d+$")

_XDG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
CONFIG_DIR = _XDG / "fmpresence"
ENV_FILE = CONFIG_DIR / "env"
UNIT_DIR = _XDG / "systemd" / "user"
UNIT_FILE = UNIT_DIR / "fmpresence.service"


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


def spotify_search_url(artist: str, title: str) -> str:
    return "https://open.spotify.com/search/" + urllib.parse.quote(f"{artist} {title}")


def get_json(url: str, headers: dict = None, timeout: float = 10) -> dict:
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
    """Memo for network lookups. Failures are cached too, so a miss costs one call."""

    def __init__(self, size: int = 2000) -> None:
        self.size = size
        self.data: dict = {}

    def get(self, key: tuple, fn):
        if key not in self.data:
            if len(self.data) >= self.size:
                self.data.clear()
            try:
                self.data[key] = fn()
            except Exception as e:
                log.debug("lookup %s failed: %s", key, e)
                self.data[key] = None
        return self.data[key]


CACHE = Cache()


# ---------------------------------------------------------------- config

class Config:
    def __init__(self, env=None) -> None:
        e = os.environ if env is None else env

        def g(k: str, d: str = "") -> str:
            return (e.get(k) or d).strip()

        self.discord_client_id = g("DISCORD_CLIENT_ID")
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
        "paused": False,
        "duration": 0,
        "offset": None,  # unknown: count from when we first saw it
    }


def lastfm_now_playing(cfg: Config):
    t = parse_lastfm_recent(lastfm(cfg, "user.getrecenttracks", user=cfg.lastfm_user, limit="1"))
    if t:
        ms = CACHE.get(
            ("dur", t["artist"], t["title"]),
            lambda: int(lastfm(cfg, "track.getInfo", artist=t["artist"], track=t["title"], autocorrect="1")["track"].get("duration") or 0),
        )
        t["duration"] = (ms or 0) // 1000
    return t


def lastfm_album_art(cfg: Config, artist: str, album: str, title: str) -> str:
    if not cfg.lastfm_api_key:
        return ""

    def look() -> str:
        if album:
            try:
                art = lastfm_image(lastfm(cfg, "album.getInfo", artist=artist, album=album, autocorrect="1")["album"].get("image", []))
                if art:
                    return art
            except Exception:
                pass
        t = lastfm(cfg, "track.getInfo", artist=artist, track=title, autocorrect="1")["track"]
        return lastfm_image(t.get("album", {}).get("image", []))

    return CACHE.get(("lfmart", artist, album, title), look) or ""


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
    if t.get("thumb"):
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
    return t


# ---------------------------------------------------------------- presence state

def clip(s: str, n: int = 128) -> str:
    # Discord rejects fields shorter than 2 or longer than 128 chars
    s = (s or "").strip()
    if len(s) < 2:
        s = (s + "  ")[:2]
    return s if len(s) <= n else s[: n - 1] + "…"


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
            if changed or abs(s - self.started) > 4:
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
        display = {"name": StatusDisplayType.NAME, "state": StatusDisplayType.STATE}.get(c.status_display, StatusDisplayType.DETAILS)
        kw = dict(
            activity_type=ActivityType.WATCHING if video else ActivityType.LISTENING,
            status_display_type=display,
            details=clip(t["title"]),
            state=clip(t["artist"] + (" · paused" if t["paused"] else "")),
            large_image=t.get("art") or None,
            large_text=clip(t["album"]) if t.get("album") else None,
        )
        buttons = []
        if t.get("link"):
            buttons.append({"label": "Listen", "url": t["link"]})
        if t.get("url"):
            buttons.append({"label": "View on Last.fm", "url": t["url"]})
        if buttons:
            kw["buttons"] = buttons[:2]
        if c.spotify_icon and not video:
            kw.update(small_image=c.spotify_icon_url, small_text="Find on Spotify",
                      small_url=spotify_search_url(t["artist"], t["title"]))
        if not t["paused"]:
            kw["start"] = int(self.started)
            if t["duration"]:
                kw["end"] = int(self.started + t["duration"])
        if c.app_name:
            kw["name"] = c.app_name
        return {k: v for k, v in kw.items() if v is not None}


class RPC:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.rpc = None

    def ensure(self) -> bool:
        if self.rpc:
            return True
        try:
            from pypresence import Presence

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
    if not cfg.discord_client_id or not (cfg.use_plex or cfg.use_lastfm):
        log.error("need DISCORD_CLIENT_ID plus PLEX_URL+PLEX_TOKEN and/or LASTFM_USER+LASTFM_API_KEY (see %s)", ENV_FILE)
        return 2

    plex_user = cfg.plex_user
    if cfg.use_plex and not plex_user:
        try:
            plex_user = plex_owner(cfg)
        except Exception as e:
            log.warning("could not resolve Plex account (%s); showing any user's session", e)
    srcs = ([f"plex ({plex_user or 'any user'})"] if cfg.use_plex else []) + (
        [f"last.fm ({cfg.lastfm_user})"] if cfg.use_lastfm else []
    )
    log.info("sources: %s | art proxy: %s", " -> ".join(srcs), cfg.art_base_url or "off")

    wake = threading.Event()
    watcher = None
    if cfg.use_plex and cfg.plex_websocket:
        watcher = PlexWatcher(cfg, wake)
        watcher.start()

    rpc = RPC(cfg.discord_client_id)
    tracker = Tracker(cfg)
    signal.signal(signal.SIGTERM, lambda *_: (rpc.call("clear"), rpc.drop(), sys.exit(0)))

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
        # Last.fm only matters when Plex is idle, and it changes slowly
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

        if track:
            track = enrich(cfg, track)
            if not track.get("art"):
                CACHE.get(("noart", track["id"]),
                          lambda: log.info("no album art found for %s - %s", track["artist"], track["title"]))
        changed, payload = tracker.step(track, now)

        if payload is not None:
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

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def do_GET(self):
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
# ART_BASE_URL=https://art.example.com
# ART_SECRET=
"""

UNIT_TEMPLATE = """\
[Unit]
Description=fmpresence: Plex/Last.fm now playing -> Discord Rich Presence
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
    if cmd == "run":
        return run(Config())
    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
