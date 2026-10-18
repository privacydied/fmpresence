# fmpresence

Shows what you are playing as a Discord Rich Presence (**Listening to …**).

It has two sources:

- **Plex** (recommended for Plex users): reads your Plex Media Server session
  directly, so updates are immediate and progress is exact.
- **Last.fm**: works with any player that sends **now playing** to Last.fm.

When both are configured, Plex is checked first. Last.fm is used only when
nothing is playing on Plex.

It runs as a `systemd --user` service. It talks to the Discord desktop client
through the local IPC socket. It does not need a Discord bot or a token.

## What it shows

- Track title and artist
- Album art from Last.fm, with the album name on hover
- A progress bar (Plex always knows the track length; Last.fm only sometimes)
- A **View on Last.fm** button

When nothing is playing, the presence is cleared. When Plex is paused, the
presence is also cleared, unless you set `SHOW_PAUSED=1`.

## How it works

Every `POLL_SECONDS` (default 15):

1. **Plex:** it reads `/status/sessions` and takes the music session of your
   Plex user. The playback position gives the progress bar. Discord cannot load
   images from a LAN Plex server, so the album art comes from Last.fm
   (`album.getInfo`) when a Last.fm API key is set.
2. **Last.fm:** if Plex has nothing, it calls `user.getRecentTracks` with
   `limit=1`. It uses the newest track only if it has the `nowplaying` flag.
3. It sends the track to Discord again on every poll, so the presence comes
   back after a Discord restart. When nothing is playing, it clears the
   presence.

> **Note for Plex users:** Plex's built-in Last.fm scrobbler sends only
> finished scrobbles, not "now playing". With Plex, use the Plex source.

Error handling:

- **Discord is not running:** it tries to connect again on every poll.
- **All sources fail:** it waits longer after each failure (exponential backoff,
  maximum 5 minutes).
- **Config is missing:** it exits with code 2. systemd does not restart it.

## Requirements

- Linux with the Discord desktop client running. The IPC socket is at
  `$XDG_RUNTIME_DIR/discord-ipc-0`.
- In Discord, **Settings → Activity Privacy → Share my activity** must be on.
- Python 3.14 or later (set in `pyproject.toml`) and [uv](https://docs.astral.sh/uv/)
- A Discord application ID
- For the Plex source: your Plex server URL and a Plex token
- For the Last.fm source and for album art: a Last.fm API key
- For the Last.fm source only: your player must send **now playing** to
  Last.fm. To check, play a track and look at your Last.fm profile. It must
  show "Scrobbling now".

## Setup

### 1. Install dependencies

```bash
git clone https://github.com/privacydied/fmpresence.git
cd fmpresence
uv sync
```

### 2. Get credentials

- **Last.fm API key:** create one at <https://www.last.fm/api/account/create>.
  You only need the API key. You do not need the shared secret.
- **Plex token:** in Plex Web, open any item, select **⋯ → Get Info → View
  XML**, and copy the `X-Plex-Token=` value from the URL. On the server, the
  token is also the `PlexOnlineToken` attribute in `Preferences.xml`.
- **Discord application ID:** open <https://discord.com/developers/applications>,
  click **New Application**, and copy the **Application ID**. The application
  name is the text after "Listening to", so give it a name like `Plex`.

### 3. Configure

Create `~/.config/fmpresence/env` and set its mode to 600, because it contains
your API key:

```ini
DISCORD_CLIENT_ID=123456789012345678
PLEX_URL=http://your-plex-server:32400
PLEX_TOKEN=your_plex_token
LASTFM_USER=your_lastfm_username
LASTFM_API_KEY=your_api_key
POLL_SECONDS=5
# PLEX_USER=someone   # optional: Plex username to follow (default: the token owner)
# SHOW_PAUSED=1       # optional: keep the presence while paused
# APP_NAME=Plex      # optional: replaces the application name in "Listening to <name>"
# LOG_LEVEL=DEBUG
```

| Variable            | Required | Default | Purpose                                  |
|---------------------|----------|---------|------------------------------------------|
| `DISCORD_CLIENT_ID` | yes      |         | Discord application ID                   |
| `PLEX_URL`          | *        |         | Plex server URL, for example `http://host:32400` |
| `PLEX_TOKEN`        | *        |         | Plex token                               |
| `PLEX_USER`         | no       | token owner | Plex username whose session is shown |
| `LASTFM_USER`       | *        |         | Last.fm username                         |
| `LASTFM_API_KEY`    | *        |         | Last.fm API key (also used for album art with Plex) |
| `SHOW_PAUSED`       | no       | `0`     | `1` keeps the presence while Plex is paused |
| `POLL_SECONDS`      | no       | `15`    | Time between polls in seconds (minimum 5) |
| `APP_NAME`          | no       |         | Replaces the "Listening to" name         |
| `LOG_LEVEL`         | no       | `INFO`  | Python log level                         |

\* You need at least one source: `PLEX_URL` with `PLEX_TOKEN`, or
`LASTFM_USER` with `LASTFM_API_KEY`. Discord allows about 5 presence updates
per 20 seconds, so do not set `POLL_SECONDS` below 5.

### 4. Test in the foreground

```bash
set -a; . ~/.config/fmpresence/env; set +a
.venv/bin/python fmpresence.py
```

### 5. Install the service

Run these commands from the repo directory. The `sed` command replaces the
`/path/to/fmpresence` placeholder in the unit file with the real path of the
repo:

```bash
mkdir -p ~/.config/systemd/user
sed "s|/path/to/fmpresence|$PWD|g" fmpresence.service > ~/.config/systemd/user/fmpresence.service
systemctl --user daemon-reload
systemctl --user enable --now fmpresence
journalctl --user -u fmpresence -f
```

The unit file is copied, not linked. This lets systemd load it at login even
if the repo is on a network drive that is not mounted yet.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `need DISCORD_CLIENT_ID plus …` and the service stops | A required variable in `~/.config/fmpresence/env` is empty. |
| `plex poll failed: HTTP Error 401` | The Plex token is wrong or expired. |
| Plex plays but nothing shows | The session belongs to a different Plex user. Set `PLEX_USER` to the username in the Plex session. |
| `last.fm …: Invalid API key` | The API key is wrong. Check `LASTFM_API_KEY`. |
| It runs but Discord shows nothing | Turn on **Share my activity** in Discord. Check that the Discord desktop client is running (the browser version has no IPC). |
| It never shows a track (Last.fm source) | Your player does not send "now playing" to Last.fm. Check your Last.fm profile while a track plays. Plex users: use the Plex source. |
| The album art is missing | Last.fm has no art for that album, or `LASTFM_API_KEY` is not set. The presence is still shown, without an image. |

## Files

```
fmpresence.py        the script
fmpresence.service   systemd user unit
pyproject.toml       dependencies (pypresence)
```
