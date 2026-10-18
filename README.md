# fmpresence

Shows your Last.fm "now playing" track as a Discord Rich Presence
(**Listening to …**). Use it with any player that scrobbles to Last.fm. It
was built for Plex.

It runs as a `systemd --user` service. It talks to the Discord desktop client
through the local IPC socket. It does not need a Discord bot or a token.

## What it shows

- Track title and artist
- Album art from Last.fm, with the album name on hover
- Elapsed time, and a progress bar when Last.fm knows the track length
- A **View on Last.fm** button

When nothing is playing, the presence is cleared.

## How it works

1. Every `POLL_SECONDS` (default 15), it calls `user.getRecentTracks` with `limit=1`.
2. If the newest track has the `nowplaying` flag, it sends the track to Discord.
   It sends the track again on every poll, so the presence comes back after a
   Discord restart.
3. When the track has no `nowplaying` flag, it clears the presence.

Error handling:

- **Discord is not running:** it tries to connect again on every poll.
- **Last.fm fails:** it waits longer after each failure (exponential backoff,
  maximum 5 minutes).
- **Config is missing:** it exits with code 2. systemd does not restart it.

## Requirements

- Linux with the Discord desktop client running. The IPC socket is at
  `$XDG_RUNTIME_DIR/discord-ipc-0`.
- In Discord, **Settings → Activity Privacy → Share my activity** must be on.
- Python 3.14 or later (set in `pyproject.toml`) and [uv](https://docs.astral.sh/uv/)
- A Last.fm API key
- A Discord application ID
- Your player must send **now playing** to Last.fm, not only scrobbles. To
  check, play a track and look at your Last.fm profile. It must show
  "Scrobbling now".

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
- **Discord application ID:** open <https://discord.com/developers/applications>,
  click **New Application**, and copy the **Application ID**. The application
  name is the text after "Listening to", so give it a name like `Plex`.

### 3. Configure

Create `~/.config/fmpresence/env` and set its mode to 600, because it contains
your API key:

```ini
LASTFM_USER=your_lastfm_username
LASTFM_API_KEY=your_api_key
DISCORD_CLIENT_ID=123456789012345678
POLL_SECONDS=15
# APP_NAME=Plex      # optional: replaces the application name in "Listening to <name>"
# LOG_LEVEL=DEBUG
```

| Variable            | Required | Default | Purpose                                  |
|---------------------|----------|---------|------------------------------------------|
| `LASTFM_USER`       | yes      |         | Last.fm username                         |
| `LASTFM_API_KEY`    | yes      |         | Last.fm API key                          |
| `DISCORD_CLIENT_ID` | yes      |         | Discord application ID                   |
| `POLL_SECONDS`      | no       | `15`    | Time between polls in seconds (minimum 5) |
| `APP_NAME`          | no       |         | Replaces the "Listening to" name         |
| `LOG_LEVEL`         | no       | `INFO`  | Python log level                         |

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
| `missing env: …` and the service stops | A required variable in `~/.config/fmpresence/env` is empty. |
| `last.fm …: Invalid API key` | The API key is wrong. Check `LASTFM_API_KEY`. |
| It runs but Discord shows nothing | Turn on **Share my activity** in Discord. Check that the Discord desktop client is running (the browser version has no IPC). |
| It never shows a track | Your player does not send "now playing" to Last.fm. Check your Last.fm profile while a track plays. |
| The album art is missing | Last.fm has no art for that track. The presence is still shown, without an image. |

## Files

```
fmpresence.py        the script
fmpresence.service   systemd user unit
pyproject.toml       dependencies (pypresence)
```
