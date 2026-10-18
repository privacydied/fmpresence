"""Offline tests. Fixtures are real API responses (trimmed); variants are derived in code."""

import copy
import json
from pathlib import Path

import pytest

import fmpresence as f

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def cfg(**env):
    base = {"DISCORD_CLIENT_ID": "1", "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "t"}
    base.update(env)
    return f.Config(base)


@pytest.fixture
def plex():
    return load("plex_sessions_track.json")


@pytest.fixture
def user(plex):
    return plex["MediaContainer"]["Metadata"][0]["User"]["title"]


# ---------------------------------------------------------------- helpers

def test_norm_strips_noise():
    assert f.norm("Tracey Morgan Vomit (Remastered) [Explicit]") == "tracey morgan vomit"
    assert f.norm("Rain Dance feat. Errol Holden") == "rain dance"
    assert f.norm("Beyoncé") == "beyonce"


def test_split_artists():
    assert f.split_artists("Ajmaa Lahcen;Ahwach Ntfarkhin") == "Ajmaa Lahcen, Ahwach Ntfarkhin"
    assert f.split_artists("Solo") == "Solo"


def test_clip_limits():
    assert f.clip("a") == "a "
    assert len(f.clip("x" * 300)) == 128


# ---------------------------------------------------------------- Plex

def test_plex_track_parse(plex, user):
    t = f.parse_plex_sessions(plex, user)
    m = plex["MediaContainer"]["Metadata"][0]
    assert t["kind"] == "music" and t["source"] == "plex"
    assert t["title"] == m["title"]
    assert t["album"] == m["parentTitle"]
    assert t["duration"] == m["duration"] // 1000
    assert t["offset"] == m["viewOffset"] / 1000
    assert t["paused"] is False
    assert f.THUMB_RE.match(t["thumb"])


def test_plex_other_user_ignored(plex):
    assert f.parse_plex_sessions(plex, "somebody-else") is None


def test_plex_paused(plex, user):
    plex["MediaContainer"]["Metadata"][0]["Player"]["state"] = "paused"
    assert f.parse_plex_sessions(plex, user)["paused"] is True


def test_plex_prefers_playing_session(plex, user):
    paused = copy.deepcopy(plex["MediaContainer"]["Metadata"][0])
    paused.update(title="Old", sessionKey="99")
    paused["Player"]["state"] = "paused"
    plex["MediaContainer"]["Metadata"].insert(0, paused)
    assert f.parse_plex_sessions(plex, user)["title"] != "Old"


def test_plex_episode_needs_show_video(plex, user):
    ep = plex["MediaContainer"]["Metadata"][0]
    ep.update(type="episode", grandparentTitle="The Wire", title="The Target", parentIndex=1, index=1)
    assert f.parse_plex_sessions(plex, user) is None
    t = f.parse_plex_sessions(plex, user, show_video=True)
    assert t["kind"] == "video" and t["title"] == "The Wire" and t["artist"] == "S01E01 · The Target"


def test_plex_empty():
    assert f.parse_plex_sessions({"MediaContainer": {"size": 0}}, "x") is None


# ---------------------------------------------------------------- Last.fm

def test_lastfm_idle_is_none():
    assert f.parse_lastfm_recent(load("lastfm_recent_idle.json")) is None


def test_lastfm_now_playing():
    d = load("lastfm_recent_idle.json")
    tr = d["recenttracks"]["track"]
    first = tr[0] if isinstance(tr, list) else tr
    first["@attr"] = {"nowplaying": "true"}
    first["artist"]["#text"] = "A;B"
    t = f.parse_lastfm_recent(d)
    assert t["artist"] == "A, B" and t["offset"] is None and t["source"] == "lastfm"


# ---------------------------------------------------------------- iTunes

def test_itunes_pick_real_result():
    r = f.itunes_pick(load("itunes_easy_bake_oven.json")["results"], "Roc Marciano", "Easy Bake Oven", "656")
    assert r and r["trackName"] == "Easy Bake Oven"


def test_itunes_rejects_wrong_artist():
    assert f.itunes_pick(load("itunes_easy_bake_oven.json")["results"], "Someone Else", "Easy Bake Oven") is None


def test_itunes_rejects_partial_word_title():
    res = [{"trackName": "Easy Bake Ovenly", "artistName": "Roc Marciano", "trackId": 1}]
    assert f.itunes_pick(res, "Roc Marciano", "Easy Bake Oven") is None


# ---------------------------------------------------------------- art signing

def test_art_sign_roundtrip():
    tok = f.art_sign("s3cret", "/library/metadata/1/thumb/2")
    assert f.art_verify("s3cret", tok) == "/library/metadata/1/thumb/2"
    assert f.art_verify("wrong", tok) is None


def test_art_rejects_non_library_paths_even_if_signed():
    assert f.art_verify("s", f.art_sign("s", "http://evil/x")) is None
    assert f.art_verify("s", "garbage") is None


def test_plex_art_url_needs_config():
    assert f.plex_art_url(cfg(), "/library/metadata/1/thumb/2") == ""
    u = f.plex_art_url(cfg(ART_BASE_URL="https://art.example/", ART_SECRET="s"), "/library/metadata/1/thumb/2")
    assert u.startswith("https://art.example/a/") and u.endswith(".jpg")


# ---------------------------------------------------------------- hide list

def test_hide_rules(plex, user):
    t = f.parse_plex_sessions(plex, user)
    assert not f.hidden(cfg(), t)
    assert f.hidden(cfg(HIDE_ARTISTS="nobody|" + t["artist"].upper()), t)
    assert f.hidden(cfg(HIDE_ALBUMS=t["album"]), t)
    assert f.hidden(cfg(HIDE_LIBRARIES=t["library"]), t)
    multi = dict(t, artist="X, Secret Guy")
    assert f.hidden(cfg(HIDE_ARTISTS="secret guy"), multi)


# ---------------------------------------------------------------- tracker

def track(**kw):
    t = {"kind": "music", "source": "plex", "id": "plex:1:1", "title": "Song", "artist": "Artist",
         "album": "Album", "album_artist": "Artist", "library": "", "art": "", "url": "", "link": "",
         "paused": False, "duration": 200, "offset": 10.0}
    t.update(kw)
    return t


def test_tracker_progress_bar_from_offset():
    tr = f.Tracker(cfg())
    changed, p = tr.step(track(offset=10.0), now=1000)
    assert changed and p["start"] == 990 and p["end"] == 1190


def test_tracker_member_list_shows_track_by_default():
    from pypresence.types import StatusDisplayType

    _, p = f.Tracker(cfg()).step(track(), now=1000)
    assert p["status_display_type"] == StatusDisplayType.DETAILS
    _, p = f.Tracker(cfg(STATUS_DISPLAY="state")).step(track(), now=1000)
    assert p["status_display_type"] == StatusDisplayType.STATE


def test_tracker_steady_between_polls():
    tr = f.Tracker(cfg())
    tr.step(track(offset=10.0), now=1000)
    changed, p = tr.step(track(offset=12.0), now=1003)  # Plex offsets lag a little
    assert not changed and p["start"] == 990


def test_tracker_seek_and_replay_move_bar():
    tr = f.Tracker(cfg())
    tr.step(track(offset=10.0), now=1000)
    _, p = tr.step(track(offset=150.0), now=1005)  # seek forward
    assert p["start"] == 855
    _, p = tr.step(track(offset=1.0), now=1010)  # replayed from the top
    assert p["start"] == 1009


def test_tracker_lastfm_replay_after_duration():
    tr = f.Tracker(cfg())
    lf = dict(source="lastfm", id="lastfm:a:s", offset=None, duration=100)
    tr.step(track(**lf), now=1000)
    _, p = tr.step(track(**lf), now=1050)
    assert p["start"] == 1000
    _, p = tr.step(track(**lf), now=1200)  # ended + 15 s and still now playing
    assert p["start"] == 1200


def test_tracker_pause_clears_unless_configured():
    _, p = f.Tracker(cfg()).step(track(paused=True), now=1)
    assert p is None
    _, p = f.Tracker(cfg(SHOW_PAUSED="1")).step(track(paused=True), now=1)
    assert p["state"].endswith("paused") and "start" not in p


def test_tracker_buttons():
    _, p = f.Tracker(cfg()).step(track(link="https://song.link/i/1", url="https://last.fm/x"), now=1)
    assert [b["label"] for b in p["buttons"]] == ["Listen", "View on Last.fm"]


def test_tracker_hidden_clears():
    tr = f.Tracker(cfg(HIDE_ARTISTS="artist"))
    changed, p = tr.step(track(), now=1)
    assert p is None


def test_video_is_watching():
    from pypresence.types import ActivityType

    _, p = f.Tracker(cfg()).step(track(kind="video", title="The Wire", artist="S01E01"), now=1)
    assert p["activity_type"] == ActivityType.WATCHING


# ---------------------------------------------------------------- enrich (no network)

def test_enrich_prefers_plex_proxy_then_itunes(monkeypatch, plex, user):
    monkeypatch.setattr(f, "itunes_lookup", lambda *a: {"art": "https://itunes/art.jpg", "link": "https://song.link/i/9"})
    monkeypatch.setattr(f, "lastfm_album_art", lambda *a: pytest.fail("should not need last.fm"))
    t = f.enrich(cfg(), f.parse_plex_sessions(plex, user))
    assert t["art"] == "https://itunes/art.jpg" and t["link"] == "https://song.link/i/9"
    t = f.enrich(cfg(ART_BASE_URL="https://art.x", ART_SECRET="s"), f.parse_plex_sessions(plex, user))
    assert t["art"].startswith("https://art.x/a/")


def test_enrich_falls_back_to_lastfm(monkeypatch, plex, user):
    monkeypatch.setattr(f, "itunes_lookup", lambda *a: None)
    monkeypatch.setattr(f, "lastfm_album_art", lambda *a: "https://lastfm/art.jpg")
    t = f.enrich(cfg(), f.parse_plex_sessions(plex, user))
    assert t["art"] == "https://lastfm/art.jpg" and t["link"] == ""


def test_config_defaults():
    c = cfg()
    assert c.use_plex and not c.use_lastfm
    assert c.poll == 15 and c.lastfm_poll == 30 and c.status_display == "details"
    assert c.plex_websocket and not c.show_video and not c.spotify_icon
