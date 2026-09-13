# Discord Music Bot

A Discord bot that plays audio from YouTube in a voice channel, with a per-server
queue and slash commands.

## Commands

| Command | Description |
|---|---|
| `/play <query>` | Plays a YouTube link (or search terms) immediately, or adds it to the queue if something is already playing |
| `/skip` | Skips the current song |
| `/next` | Alias for `/skip` |
| `/stop` | Stops playback, clears the queue, and disconnects |
| `/pause` | Pauses the current song |
| `/resume` | Resumes a paused song |
| `/queue` | Shows what's playing and what's up next (first 10) |
| `/nowplaying` | Shows details about the current song |
| `/remove <position>` | Removes a specific song from the queue |
| `/clear` | Empties the queue without stopping the current song |
| `/shuffle` | Shuffles the queue order |
| `/loop <off/song/queue>` | Loops the current song or the whole queue |
| `/volume <0-100>` | Sets playback volume |
| `/join` | Joins your current voice channel |
| `/leave` | Leaves the voice channel |

The bot also auto-leaves a voice channel if everyone else leaves it (after a
30-second grace period), so it doesn't sit idle in an empty channel.

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html) installed and available on your system `PATH`
- A Discord bot application and token

## Setup

### 1. Create a Discord application/bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Go to the **Bot** tab, click **Add Bot**, then **Reset Token** to get your bot token (keep this secret).
3. Under **Privileged Gateway Intents**, you don't need to enable "Message Content" since this bot uses slash commands only.
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Connect`, `Speak`, `Send Messages`, `Embed Links`, `Read Message History`
5. Open the generated URL and invite the bot to your server.

### 2. Install FFmpeg

- **Windows**: download from ffmpeg.org, extract, and add the `bin` folder to your `PATH`. Or `choco install ffmpeg`.
- **macOS**: `brew install ffmpeg`
- **Linux (Debian/Ubuntu)**: `sudo apt install ffmpeg`

### 3. Install the bot

```bash
# (optional) create a virtual environment
python3 -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate

pip install -r requirements.txt
```

### 4. Configure your token

```bash
cp .env.example .env
```

Then edit `.env` and paste your bot token:

```
DISCORD_TOKEN=your-actual-token-here
```

### 5. Run it

```bash
python bot.py
```

Slash commands are synced automatically on startup. It can take up to an hour
for global slash commands to fully propagate the first time, though usually
it's near-instant.

## Project structure

```
discord-music-bot/
├── bot.py              # Entry point, loads the cog and syncs slash commands
├── cogs/
│   └── music.py        # All music commands, queue, and playback logic
├── requirements.txt
├── .env.example
└── README.md
```

## Notes & possible extensions

- **Keep yt-dlp updated.** YouTube changes frequently break extraction; run
  `pip install -U yt-dlp` periodically if `/play` starts failing.
- **Respect YouTube's Terms of Service** and copyright law for how you use
  streamed audio (e.g. personal/private use in a small server vs. broader
  distribution).
- Ideas for further extensions: a DJ role that restricts who can skip/stop,
  Spotify link support (resolve track name → search YouTube), an autoplay
  mode that queues related videos when the queue empties, per-user queue
  limits, or a `/lyrics` command via a lyrics API.
