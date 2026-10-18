#!/usr/bin/env python3
"""Last.fm now-playing -> Discord Rich Presence ("Listening to ...")."""

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

API = "https://ws.audioscrobbler.com/2.0/"
# Last.fm's grey-star placeholder when a track has no art
PLACEHOLDER_ART = "2a96cbd8b46e442fc41c2b86b821562f"

USER = os.environ.get("LASTFM_USER", "")
API_KEY = os.environ.get("LASTFM_API_KEY", "")
CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
POLL = max(5, int(os.environ.get("POLL_SECONDS", "15")))
APP_NAME = os.environ.get("APP_NAME", "")  # optional: overrides the "Listening to <name>" text

log = logging.getLogger("fmpresence")


def lastfm(method: str, **params: str) -> dict:
    q = urllib.parse.urlencode({"method": method, "api_key": API_KEY, "format": "json", **params})
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": "fmpresence/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    if "error" in data:
        raise RuntimeError(f"last.fm {method}: {data.get('message')} ({data['error']})")
    return data


def now_playing() -> dict | None:
    tracks = lastfm("user.getrecenttracks", user=USER, limit="1", extended="0")["recenttracks"]["track"]
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not tracks or tracks[0].get("@attr", {}).get("nowplaying") != "true":
        return None
    t = tracks[0]
    art = next((i["#text"] for i in reversed(t.get("image", [])) if i.get("#text")), "")
    return {
        "title": t["name"],
        # Plex scrobbles multiple artists joined with ";"
        "artist": ", ".join(a.strip() for a in t["artist"]["#text"].split(";") if a.strip()),
        "album": t.get("album", {}).get("#text", ""),
        "art": "" if PLACEHOLDER_ART in art else art,
        "url": t.get("url", ""),
    }


_duration_cache: dict[tuple[str, str], int] = {}


def duration_s(artist: str, title: str) -> int:
    key = (artist, title)
    if key not in _duration_cache:
        try:
            ms = int(lastfm("track.getInfo", artist=artist, track=title, autocorrect="1")["track"].get("duration") or 0)
        except Exception:
            ms = 0
        _duration_cache[key] = ms // 1000
    return _duration_cache[key]


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


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    missing = [k for k, v in {"LASTFM_USER": USER, "LASTFM_API_KEY": API_KEY, "DISCORD_CLIENT_ID": CLIENT_ID}.items() if not v]
    if missing:
        log.error("missing env: %s", ", ".join(missing))
        return 2

    rpc = RPC()
    signal.signal(signal.SIGTERM, lambda *_: (rpc.call("clear"), rpc.drop(), sys.exit(0)))

    shown: tuple | None = None  # what Discord currently displays
    started = 0
    backoff = POLL
    while True:
        try:
            track = now_playing()
            backoff = POLL
        except Exception as e:
            log.warning("last.fm poll failed: %s", e)
            backoff = min(backoff * 2, 300)
            time.sleep(backoff)
            continue

        key = (track["artist"], track["title"]) if track else None
        # re-send every poll while playing (cheap; also notices a restarted Discord)
        if track or key != shown:
            if track:
                if key != shown:
                    started = int(time.time())
                dur = duration_s(*key)
                kw = dict(
                    activity_type=ActivityType.LISTENING,
                    status_display_type=StatusDisplayType.STATE,
                    details=clip(track["title"]),
                    state=clip(track["artist"]),
                    start=started,
                    large_image=track["art"] or None,
                    large_text=clip(track["album"]) if track["album"] else None,
                    buttons=[{"label": "View on Last.fm", "url": track["url"]}] if track["url"] else None,
                )
                if dur:
                    kw["end"] = started + dur
                if APP_NAME:
                    kw["name"] = APP_NAME
                if rpc.call("update", **{k: v for k, v in kw.items() if v is not None}):
                    if key != shown:
                        log.info("now playing: %s - %s", track["artist"], track["title"])
                    shown = key
            else:
                if rpc.call("clear"):
                    log.info("nothing playing, cleared")
                    shown = None
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
