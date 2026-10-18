#!/usr/bin/env python3
"""Now playing (Plex sessions and/or Last.fm) -> Discord Rich Presence ("Listening to ...")."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import urllib.parse
import urllib.request

from pypresence import Presence
from pypresence.exceptions import PyPresenceException
from pypresence.types import ActivityType, StatusDisplayType

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
# Last.fm's grey-star placeholder when a track has no art
PLACEHOLDER_ART = "2a96cbd8b46e442fc41c2b86b821562f"

USER = os.environ.get("LASTFM_USER", "")
API_KEY = os.environ.get("LASTFM_API_KEY", "")
CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_USER = os.environ.get("PLEX_USER", "")  # default: the token's own account
SHOW_PAUSED = os.environ.get("SHOW_PAUSED", "0").lower() in ("1", "true", "yes")
POLL = max(5, int(os.environ.get("POLL_SECONDS", "15")))
APP_NAME = os.environ.get("APP_NAME", "")  # optional: overrides the "Listening to <name>" text

log = logging.getLogger("fmpresence")


def get_json(url: str, headers: dict | None = None) -> dict:
    h = {"User-Agent": "fmpresence/1.0", "Accept": "application/json", **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=10) as r:
        return json.load(r)


def split_artists(s: str) -> str:
    # Plex joins multiple artists with ";"
    return ", ".join(a.strip() for a in s.split(";") if a.strip())


# ---------------------------------------------------------------- Last.fm

def lastfm(method: str, **params: str) -> dict:
    q = urllib.parse.urlencode({"method": method, "api_key": API_KEY, "format": "json", **params})
    data = get_json(f"{LASTFM_API}?{q}")
    if "error" in data:
        raise RuntimeError(f"last.fm {method}: {data.get('message')} ({data['error']})")
    return data


def best_image(images: list) -> str:
    art = next((i["#text"] for i in reversed(images or []) if i.get("#text")), "")
    return "" if PLACEHOLDER_ART in art else art


def lastfm_url(artist: str, title: str) -> str:
    q = urllib.parse.quote_plus
    return f"https://www.last.fm/music/{q(artist)}/_/{q(title)}"


_cache: dict[tuple, object] = {}


def cached(key: tuple, fn):
    if key not in _cache:
        if len(_cache) > 2000:
            _cache.clear()
        try:
            _cache[key] = fn()
        except Exception as e:
            log.debug("lookup %s failed: %s", key, e)
            _cache[key] = None
    return _cache[key]


def lastfm_duration(artist: str, title: str) -> int:
    ms = cached(
        ("dur", artist, title),
        lambda: int(lastfm("track.getInfo", artist=artist, track=title, autocorrect="1")["track"].get("duration") or 0),
    )
    return (ms or 0) // 1000


def album_art(artist: str, album: str, title: str) -> str:
    """Public cover URL from Last.fm (Discord can't load LAN-only Plex thumbs)."""
    if not API_KEY:
        return ""

    def look() -> str:
        if album:
            try:
                art = best_image(lastfm("album.getInfo", artist=artist, album=album, autocorrect="1")["album"].get("image", []))
                if art:
                    return art
            except Exception:
                pass
        t = lastfm("track.getInfo", artist=artist, track=title, autocorrect="1")["track"]
        return best_image(t.get("album", {}).get("image", []))

    return cached(("art", artist, album, title), look) or ""


def lastfm_now_playing() -> dict | None:
    tracks = lastfm("user.getrecenttracks", user=USER, limit="1")["recenttracks"]["track"]
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not tracks or tracks[0].get("@attr", {}).get("nowplaying") != "true":
        return None
    t = tracks[0]
    artist, title = split_artists(t["artist"]["#text"]), t["name"]
    return {
        "source": "lastfm",
        "title": title,
        "artist": artist,
        "album": t.get("album", {}).get("#text", ""),
        "art": best_image(t.get("image", [])),
        "url": t.get("url") or lastfm_url(artist, title),
        "paused": False,
        "duration": lastfm_duration(artist, title),
        "offset": None,  # unknown: count from when we first saw it
    }


# ---------------------------------------------------------------- Plex

def plex_owner() -> str:
    """Token owner's Plex username (sessions report User.title as the username, not the display name)."""
    d = get_json("https://plex.tv/api/v2/user", {"X-Plex-Token": PLEX_TOKEN, "X-Plex-Client-Identifier": "fmpresence"})
    return d.get("username") or d.get("title") or ""


def plex_now_playing(user: str) -> dict | None:
    mc = get_json(f"{PLEX_URL}/status/sessions", {"X-Plex-Token": PLEX_TOKEN})["MediaContainer"]
    sessions = [
        m for m in mc.get("Metadata", [])
        if m.get("type") == "track" and (not user or m.get("User", {}).get("title", "").lower() == user.lower())
    ]
    if not sessions:
        return None
    # prefer an actively playing session over paused/buffering ones
    sessions.sort(key=lambda m: m.get("Player", {}).get("state") != "playing")
    m = sessions[0]
    title = m.get("title", "")
    album = m.get("parentTitle", "")
    album_artist = split_artists(m.get("grandparentTitle") or "")
    artist = split_artists(m.get("originalTitle") or "") or album_artist
    return {
        "source": "plex",
        "title": title,
        "artist": artist,
        "album": album,
        "art": album_art(album_artist or artist, album, title),
        "url": lastfm_url(artist, title),
        "paused": m.get("Player", {}).get("state") != "playing",
        "duration": int(m.get("duration") or 0) // 1000,
        "offset": int(m.get("viewOffset") or 0) / 1000,
    }


# ---------------------------------------------------------------- Discord

def clip(s: str, n: int = 128) -> str:
    # Discord rejects fields <2 or >128 chars
    s = s.strip()
    if len(s) < 2:
        s = (s + "  ")[:2]
    return s if len(s) <= n else s[: n - 1] + "…"


class RPC:
    def __init__(self) -> None:
        self.rpc: Presence | None = None

    def ensure(self) -> bool:
        if self.rpc:
            return True
        try:
            rpc = Presence(CLIENT_ID)
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
        except (PyPresenceException, OSError, RuntimeError) as e:
            log.warning("discord %s failed, reconnecting: %s", fn, e)
            self.drop()
            return False


# ---------------------------------------------------------------- main

def poll(use_plex: bool, plex_user: str, use_lastfm: bool) -> dict | None:
    """Plex first (real-time), Last.fm as fallback. Raises only if every source failed."""
    errors = []
    if use_plex:
        try:
            t = plex_now_playing(plex_user)
            if t:
                return t
        except Exception as e:
            errors.append(f"plex: {e}")
    if use_lastfm:
        try:
            t = lastfm_now_playing()
            for e in errors:
                log.warning("poll failed: %s", e)
            return t
        except Exception as e:
            errors.append(f"last.fm: {e}")
    if errors and len(errors) == use_plex + use_lastfm:
        raise RuntimeError("; ".join(errors))
    for e in errors:
        log.warning("poll failed: %s", e)
    return None


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    use_plex = bool(PLEX_URL and PLEX_TOKEN)
    use_lastfm = bool(USER and API_KEY)
    if not CLIENT_ID or not (use_plex or use_lastfm):
        log.error("need DISCORD_CLIENT_ID plus PLEX_URL+PLEX_TOKEN and/or LASTFM_USER+LASTFM_API_KEY")
        return 2

    plex_user = PLEX_USER
    if use_plex and not plex_user:
        try:
            plex_user = plex_owner()
        except Exception as e:
            log.warning("could not resolve Plex account (%s); showing any user's session", e)
    srcs = []
    if use_plex:
        srcs.append(f"plex ({plex_user or 'any user'})")
    if use_lastfm:
        srcs.append(f"last.fm ({USER})")
    log.info("sources: %s, poll every %ss", " -> ".join(srcs), POLL)

    rpc = RPC()
    signal.signal(signal.SIGTERM, lambda *_: (rpc.call("clear"), rpc.drop(), sys.exit(0)))

    shown: tuple | None = None  # what Discord currently displays
    started = 0.0
    backoff = POLL
    while True:
        try:
            track = poll(use_plex, plex_user, use_lastfm)
            backoff = POLL
        except Exception as e:
            log.warning("poll failed: %s", e)
            backoff = min(backoff * 2, 300)
            time.sleep(backoff)
            continue

        if track and track["paused"] and not SHOW_PAUSED:
            track = None

        key = (track["source"], track["artist"], track["title"], track["paused"]) if track else None
        # re-send every poll while playing (cheap; also notices a restarted Discord)
        if track or key != shown:
            if track:
                now = time.time()
                if track["offset"] is not None:
                    s = now - track["offset"]
                    # keep the bar steady; only move it on a seek or track change
                    if key != shown or abs(s - started) > 4:
                        started = s
                elif key != shown:
                    started = now
                kw = dict(
                    activity_type=ActivityType.LISTENING,
                    status_display_type=StatusDisplayType.STATE,
                    details=clip(track["title"]),
                    state=clip(track["artist"] + (" · paused" if track["paused"] else "")),
                    large_image=track["art"] or None,
                    large_text=clip(track["album"]) if track["album"] else None,
                    buttons=[{"label": "View on Last.fm", "url": track["url"]}] if track["url"] else None,
                )
                if not track["paused"]:
                    kw["start"] = int(started)
                    if track["duration"]:
                        kw["end"] = int(started + track["duration"])
                if APP_NAME:
                    kw["name"] = APP_NAME
                if rpc.call("update", **{k: v for k, v in kw.items() if v is not None}):
                    if key != shown:
                        what = "paused" if track["paused"] else "now playing"
                        log.info("%s [%s]: %s - %s", what, track["source"], track["artist"], track["title"])
                    shown = key
            else:
                if rpc.call("clear"):
                    log.info("nothing playing, cleared")
                    shown = None
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
